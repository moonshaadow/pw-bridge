"""PipeWire connection management and registry subscription.

Uses pw_thread_loop to ensure that all PipeWire API calls are
made from the loop's thread.

This module also handles:
- binding proxies on nodes, ports, links and every Metadata object
  announced by the registry
- detection of core loss (PipeWire restart) via the `core error`
  and `global_remove(id=0)` events
- automatic reconnection.

The metadata interface is optional. It requires PipeWire >= 1.2.
When it is not available (see pw_bindings.HAVE_METADATA), the
metadata-related methods are no-ops and on_metadata_changed is never
called.

Metadata layout:

    self.metadata[metadata_id][subject][key] = (type_spa, raw_value)

PipeWire typically exposes several Metadata objects at once
('default', 'settings', 'route-settings'...). We bind them all and
keep them separate. A Metadata object that emits a removal event
(value=None) only affects its own bucket, so a key published by
another Metadata object is not clobbered.

The accessors (get_metadata / get_metadata_json) walk all Metadata
objects and return the first match. Writes are sent to every bound
Metadata; PipeWire silently ignores keys an object does not know.
"""

import ctypes
import json
import logging
import threading
from typing import Callable, Optional

from . import pw_bindings as pw

_logger = logging.getLogger(__name__)


class PipeWireRegistry:
    """Encapsulates the PipeWire connection and event listening."""

    def __init__(
            self,
            on_global_added: Callable[[int, str, dict], None],
            on_global_removed: Callable[[int], None],
            on_node_info: Optional[Callable[[int, dict], None]] = None,
            on_port_info: Optional[Callable[[int, dict], None]] = None,
            on_link_info: Optional[Callable[[int, dict], None]] = None,
            on_metadata_changed: Optional[
                Callable[[int, int, str, str, Optional[str]], None]] = None,
            on_core_lost: Optional[Callable[[], None]] = None,
            on_core_restored: Optional[Callable[[], None]] = None,
            thread_name: str = "pw_bridge"):
        self._on_global_added = on_global_added
        self._on_global_removed = on_global_removed
        self._on_node_info = on_node_info
        self._on_port_info = on_port_info
        self._on_link_info = on_link_info
        self._on_metadata_changed = on_metadata_changed
        self._on_core_lost = on_core_lost
        self._on_core_restored = on_core_restored
        self._thread_name = thread_name

        self._thread_loop = None
        self._context = None
        self._core = None
        self._registry = None

        # Per-instance listeners, owned by the C wrapper.
        self._registry_listener = None
        self._core_listener = None

        # Metadata: one proxy and one listener per Metadata object
        # announced by the registry.
        self._metadata_proxies: dict[int, object] = {}
        self._metadata_listeners: dict[int, object] = {}

        # Strong references to the ctypes callbacks. They MUST live as
        # long as the corresponding C listeners.
        self._global_cb_c = pw.PW_BRIDGE_GLOBAL_CB(self._registry_global_cb)
        self._global_remove_cb_c = pw.PW_BRIDGE_GLOBAL_REMOVE_CB(
            self._registry_global_remove_cb)
        self._core_info_cb_c = pw.PW_BRIDGE_CORE_INFO_CB(self._core_info_cb)
        self._core_done_cb_c = pw.PW_BRIDGE_CORE_DONE_CB(self._core_done_cb)
        self._core_error_cb_c = pw.PW_BRIDGE_CORE_ERROR_CB(self._core_error_cb)
        self._node_info_cb_c = pw.PW_BRIDGE_NODE_INFO_CB(self._node_info_cb)
        self._port_info_cb_c = pw.PW_BRIDGE_PORT_INFO_CB(self._port_info_cb)
        self._link_info_cb_c = pw.PW_BRIDGE_LINK_INFO_CB(self._link_info_cb)

        if pw.HAVE_METADATA:
            self._metadata_cb_c = pw.PW_BRIDGE_METADATA_PROPERTY_CB(
                self._metadata_property_cb)
        else:
            self._metadata_cb_c = None

        self._running = False
        self._core_lost = False

        # Shared state, protected by _objects_lock.
        self._objects_lock = threading.RLock()
        self.objects: dict[int, tuple[str, dict]] = {}
        self._node_listeners: dict[int, int] = {}
        self._node_proxies: dict[int, object] = {}
        self._port_listeners: dict[int, int] = {}
        self._port_proxies: dict[int, object] = {}
        self._link_listeners: dict[int, int] = {}
        self._link_proxies: dict[int, object] = {}

        # Metadata snapshot, indexed by Metadata object first:
        #   self.metadata[metadata_id][subject][key] = (type_spa, raw)
        # A removal event only pops from its own metadata_id bucket.
        self.metadata: dict[int, dict[int, dict[str, tuple[str, str]]]] = {}

        # Proxies created via create_link, waiting for their link.info
        # event so we can reconcile the real link_id.
        self._pending_link_proxies: list = []

        # Reconnection thread
        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Initialize PipeWire and start the loop."""
        pw._lib.pw_init(None, None)

        if not self._create_connection():
            raise RuntimeError("Initial PipeWire connection failed")

        self._running = True
        self._core_lost = False

        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name=f"{self._thread_name}-reconnect",
            daemon=True)
        self._reconnect_thread.start()

        _logger.info("PipeWire registry started")

    def _create_connection(self) -> bool:
        """Create the thread loop, context, core and registry."""
        name_b = self._thread_name.encode("utf-8")
        self._thread_loop = pw._lib.pw_thread_loop_new(name_b, None)
        if not self._thread_loop:
            _logger.error("Failed to create the thread loop")
            return False

        loop = pw._lib.pw_thread_loop_get_loop(self._thread_loop)
        self._context = pw._lib.pw_context_new(loop, None, 0)
        if not self._context:
            _logger.error("Failed to create the context")
            return False

        self._core = pw._lib.pw_context_connect(self._context, None, 0)
        if not self._core:
            _logger.error("Failed to connect to the core")
            return False

        # Core listener (per instance).
        self._core_listener = pw._lib_wrapper.pw_bridge_core_listener_new(
            self._core,
            self._core_info_cb_c,
            self._core_done_cb_c,
            self._core_error_cb_c,
            None,
        )
        if not self._core_listener:
            _logger.error("Failed to register the core listener")
            return False

        self._registry = pw._lib_wrapper.pw_bridge_get_registry(self._core)
        if not self._registry:
            _logger.error("Failed to retrieve the registry")
            return False

        # Registry listener (per instance). Metadata objects are
        # discovered through registry global_added events.
        self._registry_listener = (
            pw._lib_wrapper.pw_bridge_registry_listener_new(
                self._registry,
                self._global_cb_c,
                self._global_remove_cb_c,
                None,
            )
        )
        if not self._registry_listener:
            _logger.error("Failed to register the registry listener")
            return False

        ret = pw._lib.pw_thread_loop_start(self._thread_loop)
        if ret < 0:
            _logger.error("Failed to start the thread loop")
            return False

        return True

    def _bind_metadata_locked(self, metadata_id: int):
        """Bind a Metadata object and attach a listener.

        Must be called with the thread loop lock held, and only when
        the registry has announced the object. If pw.HAVE_METADATA is
        False (PipeWire < 1.2), this is a no-op.
        """
        if not pw.HAVE_METADATA:
            return
        if self._registry is None:
            return
        if metadata_id in self._metadata_proxies:
            return

        proxy = pw._lib_wrapper.pw_bridge_bind_metadata(
            self._registry, metadata_id)
        if not proxy:
            return
        listener = pw._lib_wrapper.pw_bridge_metadata_listener_new(
            proxy, self._metadata_cb_c, None, metadata_id)
        if not listener:
            pw.proxy_destroy(proxy)
            return
        self._metadata_proxies[metadata_id] = proxy
        self._metadata_listeners[metadata_id] = listener

    def _unbind_metadata_locked(self, metadata_id: int):
        listener = self._metadata_listeners.pop(metadata_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_metadata_listener_free(listener)
        proxy = self._metadata_proxies.pop(metadata_id, None)
        if proxy:
            pw.proxy_destroy(proxy)
        self.metadata.pop(metadata_id, None)

    def _destroy_connection(self):
        """Cleanly destroy the PipeWire connection.

        Every PipeWire operation that touches a proxy, a hook or the
        core is performed under the thread loop lock.
        """
        if self._thread_loop:
            pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            # 1. Unbind all proxies.
            with self._objects_lock:
                for node_id in list(self._node_listeners.keys()):
                    self._unbind_node_locked(node_id)
                for port_id in list(self._port_listeners.keys()):
                    self._unbind_port_locked(port_id)
                for link_id in list(self._link_listeners.keys()):
                    self._unbind_link_locked(link_id)
                for metadata_id in list(self._metadata_listeners.keys()):
                    self._unbind_metadata_locked(metadata_id)
                for proxy, listener in self._pending_link_proxies:
                    pw._lib_wrapper.pw_bridge_link_listener_free(listener)
                    pw.proxy_destroy(proxy)
                self._pending_link_proxies.clear()

            # 2. Force a sync so the server processes the pending
            #    destroy messages before we tear the connection down.
            if self._core is not None:
                pw._lib_wrapper.pw_bridge_core_sync(
                    self._core, pw.PW_ID_CORE, 0)

            # 3. Free the registry and core listeners while the
            #    corresponding PipeWire objects are still alive.
            if self._registry_listener:
                pw._lib_wrapper.pw_bridge_registry_listener_free(
                    self._registry_listener)
                self._registry_listener = None

            if self._core_listener:
                pw._lib_wrapper.pw_bridge_core_listener_free(
                    self._core_listener)
                self._core_listener = None
        finally:
            if self._thread_loop:
                pw._lib_wrapper.pw_bridge_thread_loop_unlock(
                    self._thread_loop)

        # 4. Stop the loop.
        if self._thread_loop:
            try:
                pw._lib.pw_thread_loop_stop(self._thread_loop)
            except Exception:
                _logger.exception("Error stopping the thread loop")

        # 5. Tear down the PipeWire connection itself.
        if self._core:
            try:
                pw._lib.pw_core_disconnect(self._core)
            except Exception:
                _logger.exception("Error disconnecting the core")
            self._core = None

        if self._context:
            try:
                pw._lib.pw_context_destroy(self._context)
            except Exception:
                _logger.exception("Error destroying the context")
            self._context = None

        if self._thread_loop:
            try:
                pw._lib.pw_thread_loop_destroy(self._thread_loop)
            except Exception:
                _logger.exception("Error destroying the thread loop")
            self._thread_loop = None

        self._registry = None

    def stop(self):
        """Cleanly stop the connection and the reconnection thread."""
        if not self._running:
            return

        self._running = False
        self._reconnect_stop.set()

        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=5.0)
            if self._reconnect_thread.is_alive():
                _logger.error("Reconnect thread did not stop in time")
            self._reconnect_thread = None

        self._destroy_connection()

        with self._objects_lock:
            self.objects.clear()
            self._node_listeners.clear()
            self._node_proxies.clear()
            self._port_listeners.clear()
            self._port_proxies.clear()
            self._link_listeners.clear()
            self._link_proxies.clear()
            self._pending_link_proxies.clear()
            self._metadata_proxies.clear()
            self._metadata_listeners.clear()
            self.metadata.clear()

        _logger.info("PipeWire registry stopped")

    # ------------------------------------------------------------------
    # Reconnection thread
    # ------------------------------------------------------------------

    def _reconnect_loop(self):
        """Monitor the connection and attempt to reconnect."""
        while not self._reconnect_stop.is_set():
            if self._core_lost:
                _logger.info("Attempting to reconnect to PipeWire...")

                self._destroy_connection()
                with self._objects_lock:
                    self.objects.clear()
                    self._node_listeners.clear()
                    self._node_proxies.clear()
                    self._port_listeners.clear()
                    self._port_proxies.clear()
                    self._link_listeners.clear()
                    self._link_proxies.clear()
                    self._pending_link_proxies.clear()
                    self._metadata_proxies.clear()
                    self._metadata_listeners.clear()
                    self.metadata.clear()

                if self._create_connection():
                    _logger.info("Reconnection to PipeWire succeeded")
                    self._core_lost = False

                    if self._on_core_restored is not None:
                        try:
                            self._on_core_restored()
                        except Exception:
                            _logger.exception("Error in on_core_restored")
                else:
                    _logger.info("Reconnection failed, retrying in 1 second")

            self._reconnect_stop.wait(timeout=1.0)

    # ------------------------------------------------------------------
    # Registry callbacks
    # ------------------------------------------------------------------

    def _registry_global_cb(
            self, user_data, id_: int, permissions: int,
            type_: bytes, version: int, props):
        type_str = type_.decode() if type_ else ""
        props_dict = props.contents.to_dict() if props else {}

        with self._objects_lock:
            self.objects[id_] = (type_str, props_dict)

        if type_str == pw.PW_TYPE_INTERFACE_Node:
            self._bind_node(id_)
        elif type_str == pw.PW_TYPE_INTERFACE_Port:
            self._bind_port(id_)
        elif type_str == pw.PW_TYPE_INTERFACE_Link:
            self._bind_link(id_)
        elif (type_str == pw.PW_TYPE_INTERFACE_Metadata
                and self._thread_loop is not None):
            pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
            try:
                self._bind_metadata_locked(id_)
            finally:
                pw._lib_wrapper.pw_bridge_thread_loop_unlock(
                    self._thread_loop)

        try:
            self._on_global_added(id_, type_str, props_dict)
        except Exception:
            _logger.exception("Error in on_global_added")

    def _registry_global_remove_cb(self, user_data, id_: int):
        with self._objects_lock:
            self.objects.pop(id_, None)
            self._unbind_node_locked(id_)
            self._unbind_port_locked(id_)
            self._unbind_link_locked(id_)
            self._unbind_metadata_locked(id_)

        if id_ == pw.PW_ID_CORE:
            if not self._core_lost:
                _logger.warning("PipeWire core lost (global_remove)")
                self._core_lost = True

                if self._on_core_lost is not None:
                    try:
                        self._on_core_lost()
                    except Exception:
                        _logger.exception("Error in on_core_lost")
            return

        try:
            self._on_global_removed(id_)
        except Exception:
            _logger.exception("Error in on_global_removed")

    def _core_info_cb(self, user_data, name, version, change_mask):
        name_s = name.decode() if name else ""
        version_s = version.decode() if version else ""
        _logger.debug("Core info: name=%s version=%s", name_s, version_s)

    def _core_done_cb(self, user_data, id_, seq):
        _logger.debug("Core done: id=%s seq=%s", id_, seq)

    def _core_error_cb(self, user_data, id_, seq, res, message):
        """Called when the core reports an error."""
        msg_str = message.decode() if message else ""
        _logger.warning("Core error: id=%s seq=%s res=%s msg=%s",
                        id_, seq, res, msg_str)

        if id_ == pw.PW_ID_CORE and res == -32:
            if not self._core_lost:
                _logger.warning("Connection loss detected via core error")
                self._core_lost = True
                with self._objects_lock:
                    self.objects.clear()
                    self._node_listeners.clear()
                    self._node_proxies.clear()
                    self._port_listeners.clear()
                    self._port_proxies.clear()
                    self._link_listeners.clear()
                    self._link_proxies.clear()
                    self._pending_link_proxies.clear()
                    self._metadata_proxies.clear()
                    self._metadata_listeners.clear()
                    self.metadata.clear()

                if self._on_core_lost is not None:
                    try:
                        self._on_core_lost()
                    except Exception:
                        _logger.exception("Error in on_core_lost")

    def _node_info_cb(self, user_data, node_id, max_in, max_out,
                      change_mask, state, error,
                      props: ctypes.POINTER(pw.spa_dict)):
        if not props:
            return
        props_dict = props.contents.to_dict()
        props_dict["_max_input_ports"] = max_in
        props_dict["_max_output_ports"] = max_out
        props_dict["_change_mask"] = change_mask
        props_dict["_state"] = state
        props_dict["_error"] = error.decode() if error else ""

        with self._objects_lock:
            existing = self.objects.get(node_id)
            if existing is not None:
                type_str, base_props = existing
                merged = dict(base_props)
                merged.update(props_dict)
                self.objects[node_id] = (type_str, merged)

        if self._on_node_info is not None:
            try:
                self._on_node_info(node_id, props_dict)
            except Exception:
                _logger.exception("Error in on_node_info")

    def _port_info_cb(self, user_data, port_id, direction, change_mask,
                      props: ctypes.POINTER(pw.spa_dict)):
        if not props:
            return
        props_dict = props.contents.to_dict()
        props_dict["_direction"] = direction
        props_dict["_change_mask"] = change_mask

        with self._objects_lock:
            existing = self.objects.get(port_id)
            if existing is not None:
                type_str, base_props = existing
                merged = dict(base_props)
                merged.update(props_dict)
                self.objects[port_id] = (type_str, merged)

        if self._on_port_info is not None:
            try:
                self._on_port_info(port_id, props_dict)
            except Exception:
                _logger.exception("Error in on_port_info")

    def _link_info_cb(self, user_data, link_id, out_node, out_port,
                      in_node, in_port, state, error,
                      props: ctypes.POINTER(pw.spa_dict)):
        if not props:
            return
        props_dict = props.contents.to_dict()
        props_dict["_output_node_id"] = out_node
        props_dict["_output_port_id"] = out_port
        props_dict["_input_node_id"] = in_node
        props_dict["_input_port_id"] = in_port
        props_dict["_state"] = state
        props_dict["_error"] = error.decode() if error else ""

        with self._objects_lock:
            if (link_id not in self._link_proxies
                    and self._pending_link_proxies):
                proxy, listener = self._pending_link_proxies.pop(0)
                self._link_proxies[link_id] = proxy
                self._link_listeners[link_id] = listener

            existing = self.objects.get(link_id)
            if existing is not None:
                type_str, base_props = existing
                merged = dict(base_props)
                merged.update(props_dict)
                self.objects[link_id] = (type_str, merged)

        if self._on_link_info is not None:
            try:
                self._on_link_info(link_id, props_dict)
            except Exception:
                _logger.exception("Error in on_link_info")

    def _metadata_property_cb(self, user_data, metadata_id, subject,
                              key, type_, value):
        """Called for each metadata entry at subscribe time, and on
        every change afterwards. `value` is None when the entry is
        being removed.

        Returns 0 (accept) as required by the PipeWire metadata
        callback contract. Failing to return an int would make ctypes
        raise "NoneType cannot be interpreted as an integer".
        """
        key_s = key.decode() if key else ""
        type_s = type_.decode() if type_ else ""
        value_s = value.decode() if value else None

        with self._objects_lock:
            meta_bucket = self.metadata.setdefault(metadata_id, {})
            subject_bucket = meta_bucket.setdefault(subject, {})
            if value_s is None:
                subject_bucket.pop(key_s, None)
                if not subject_bucket:
                    meta_bucket.pop(subject, None)
            else:
                subject_bucket[key_s] = (type_s, value_s)

        if self._on_metadata_changed is not None:
            try:
                self._on_metadata_changed(
                    metadata_id, subject, key_s, type_s, value_s)
            except Exception:
                _logger.exception("Error in on_metadata_changed")

        return 0

    # ------------------------------------------------------------------
    # Binding proxies (node / port / link)
    # ------------------------------------------------------------------

    def _bind_node(self, node_id: int):
        if self._registry is None:
            return
        with self._objects_lock:
            if node_id in self._node_proxies:
                return
            proxy = pw._lib_wrapper.pw_bridge_bind_node(
                self._registry, node_id)
            if not proxy:
                return
            listener = pw._lib_wrapper.pw_bridge_node_listener_new(
                proxy, self._node_info_cb_c, None)
            if not listener:
                pw.proxy_destroy(proxy)
                return
            self._node_proxies[node_id] = proxy
            self._node_listeners[node_id] = listener

    def _bind_port(self, port_id: int):
        if self._registry is None:
            return
        with self._objects_lock:
            if port_id in self._port_proxies:
                return
            proxy = pw._lib_wrapper.pw_bridge_bind_port(
                self._registry, port_id)
            if not proxy:
                return
            listener = pw._lib_wrapper.pw_bridge_port_listener_new(
                proxy, self._port_info_cb_c, None)
            if not listener:
                pw.proxy_destroy(proxy)
                return
            self._port_proxies[port_id] = proxy
            self._port_listeners[port_id] = listener

    def _bind_link(self, link_id: int):
        if self._registry is None:
            return
        with self._objects_lock:
            if link_id in self._link_proxies:
                return
            proxy = pw._lib_wrapper.pw_bridge_bind_link(
                self._registry, link_id)
            if not proxy:
                return
            listener = pw._lib_wrapper.pw_bridge_link_listener_new(
                proxy, self._link_info_cb_c, None)
            if not listener:
                pw.proxy_destroy(proxy)
                return
            self._link_proxies[link_id] = proxy
            self._link_listeners[link_id] = listener

    def _unbind_node_locked(self, node_id: int):
        listener = self._node_listeners.pop(node_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_node_listener_free(listener)
        proxy = self._node_proxies.pop(node_id, None)
        if proxy:
            pw.proxy_destroy(proxy)

    def _unbind_port_locked(self, port_id: int):
        listener = self._port_listeners.pop(port_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_port_listener_free(listener)
        proxy = self._port_proxies.pop(port_id, None)
        if proxy:
            pw.proxy_destroy(proxy)

    def _unbind_link_locked(self, link_id: int):
        listener = self._link_listeners.pop(link_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_link_listener_free(listener)
        proxy = self._link_proxies.pop(link_id, None)
        if proxy:
            pw.proxy_destroy(proxy)

    # ------------------------------------------------------------------
    # Metadata accessors
    # ------------------------------------------------------------------

    def get_metadata(self, subject: int, key: str):
        """Return (type_spa, raw_value) for (subject, key), or None.

        Walks all bound Metadata objects and returns the first match.
        The order is deterministic but not meaningful: any Metadata
        object holding the key is considered authoritative.
        Returns None if the metadata interface is unavailable.
        """
        if not pw.HAVE_METADATA:
            return None
        with self._objects_lock:
            for meta_bucket in self.metadata.values():
                subject_bucket = meta_bucket.get(subject)
                if not subject_bucket:
                    continue
                entry = subject_bucket.get(key)
                if entry is not None:
                    return entry
        return None

    def get_metadata_json(self, subject: int, key: str):
        """Return the JSON-decoded value for (subject, key), or None.

        Accepts two cases:

        - the entry is explicitly typed Spa:String:JSON (typical for
          values published by WirePlumber);
        - the entry has an empty SPA type (typical for values
          published by the PipeWire server itself, such as
          clock.rate). In that case, we still attempt json.loads,
          because PipeWire stores these values in JSON syntax even
          when the type is not set.

        Returns None if the entry does not exist, or if the raw value
        is not valid JSON.
        """
        entry = self.get_metadata(subject, key)
        if entry is None:
            return None
        type_s, raw = entry
        if type_s not in ("", pw.SPA_TYPE_STRING_JSON):
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def set_metadata(self, subject: int, key: str, type_spa: str,
                     raw_value) -> bool:
        """Set or clear a metadata entry.

        The value is written to every bound Metadata object.
        PipeWire silently ignores keys an object does not know.
        Pass None or empty string to remove the entry.

        Must be called from a thread other than the PipeWire loop
        thread. Returns True if at least one write succeeded.
        """
        if not pw.HAVE_METADATA:
            return False
        if not self._running or self._core_lost:
            return False
        if not self._metadata_proxies:
            return False

        key_b = key.encode("utf-8") if key else None
        type_b = type_spa.encode("utf-8") if type_spa else None
        if raw_value is None or raw_value == "":
            value_b = None
        else:
            value_b = (raw_value.encode("utf-8")
                       if isinstance(raw_value, str) else raw_value)

        any_ok = False
        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            for proxy in list(self._metadata_proxies.values()):
                ret = pw._lib_wrapper.pw_bridge_metadata_set_property(
                    proxy, subject, key_b, type_b, value_b)
                if ret >= 0:
                    any_ok = True
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

        return any_ok

    def set_metadata_json(self, subject: int, key: str, value) -> bool:
        """Convenience wrapper for JSON-typed metadata.

        Encodes `value` with json.dumps() and sends it with the SPA
        type Spa:String:JSON. Pass None to remove the entry.
        """
        if not pw.HAVE_METADATA:
            return False
        if value is None:
            return self.set_metadata(subject, key,
                                     pw.SPA_TYPE_STRING_JSON, None)
        raw = json.dumps(value)
        return self.set_metadata(subject, key,
                                 pw.SPA_TYPE_STRING_JSON, raw)

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------

    def create_link(self, out_port_id: int, in_port_id: int) -> bool:
        """Create a link between two ports."""
        if not self._running or self._core is None or self._core_lost:
            return False

        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            proxy = pw._lib_wrapper.pw_bridge_create_link(
                self._core, out_port_id, in_port_id)
            if not proxy:
                return False

            listener = pw._lib_wrapper.pw_bridge_link_listener_new(
                proxy, self._link_info_cb_c, None)
            if not listener:
                pw.proxy_destroy(proxy)
                return False

            with self._objects_lock:
                self._pending_link_proxies.append((proxy, listener))
            return True
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

    def destroy_link(self, link_id: int) -> bool:
        if not self._running or self._registry is None or self._core_lost:
            return False

        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            ret = pw._lib_wrapper.pw_bridge_registry_destroy(
                self._registry, link_id)
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

        return ret >= 0

    def rename_client(self, client_id: int, new_name: str) -> bool:
        """Rename a PipeWire client via client.update-properties."""
        if not self._running or self._registry is None or self._core_lost:
            return False

        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            proxy = pw._lib_wrapper.pw_bridge_bind_client(
                self._registry, client_id)
            if not proxy:
                return False
            try:
                builder = pw.spa_dict_from_python(
                    {"application.name": new_name})
                ret = pw._lib_wrapper.pw_bridge_client_update_properties(
                    proxy, ctypes.byref(builder.dict))
            finally:
                pw.proxy_destroy(proxy)
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

        return ret >= 0

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def find_object(self, type_: str) -> dict[int, dict]:
        with self._objects_lock:
            return {
                id_: props
                for id_, (t, props) in self.objects.items()
                if t == type_
            }

    def get_object(self, id_: int) -> Optional[tuple[str, dict]]:
        with self._objects_lock:
            return self.objects.get(id_)