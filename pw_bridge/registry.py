"""PipeWire connection management and registry subscription.

Uses pw_thread_loop to ensure that all PipeWire API calls are
made from the loop's thread.

This module also handles:
- binding proxies on nodes, ports, links, devices and every
  Metadata object announced by the registry
- reading and writing SPA_PARAM_Props on nodes (volume, mute,
  channel volumes)
- detection of core loss (PipeWire restart) via the `core error`
  and `global_remove(id=0)` events
- automatic reconnection.

The metadata interface is optional. It requires PipeWire >= 1.2.

Node props:

    self._node_params_available[node_id] = set of param ids
    self._node_params_cache[node_id] = {
        "volume": float,
        "mute": bool,
        "channelVolumes": [float, ...],
    }

The available-params set is populated by node.info events. It lets
request_node_props avoid issuing enum_params on nodes that do not
support SPA_PARAM_Props, which would otherwise cause the server to
emit "enum params failed" errors.

The cache is populated by node.param events and by explicit
requests. get_node_props reads the cache first.
"""

import collections
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
            on_node_params: Optional[Callable[[int, int, dict], None]] = None,
            on_port_info: Optional[Callable[[int, dict], None]] = None,
            on_link_info: Optional[Callable[[int, dict], None]] = None,
            on_device_info: Optional[Callable[[int, dict], None]] = None,
            on_metadata_changed: Optional[
                Callable[[int, int, str, str, Optional[str]], None]] = None,
            on_core_lost: Optional[Callable[[], None]] = None,
            on_core_restored: Optional[Callable[[], None]] = None,
            thread_name: str = "pw_bridge"):
        self._on_global_added = on_global_added
        self._on_global_removed = on_global_removed
        self._on_node_info = on_node_info
        self._on_node_params = on_node_params
        self._on_port_info = on_port_info
        self._on_link_info = on_link_info
        self._on_device_info = on_device_info
        self._on_metadata_changed = on_metadata_changed
        self._on_core_lost = on_core_lost
        self._on_core_restored = on_core_restored
        self._thread_name = thread_name

        self._thread_loop = None
        self._context = None
        self._core = None
        self._registry = None

        self._registry_listener = None
        self._core_listener = None
        self._metadata_proxies: dict[int, object] = {}
        self._metadata_listeners: dict[int, object] = {}

        self._global_cb_c = pw.PW_BRIDGE_GLOBAL_CB(self._registry_global_cb)
        self._global_remove_cb_c = pw.PW_BRIDGE_GLOBAL_REMOVE_CB(
            self._registry_global_remove_cb)
        self._core_info_cb_c = pw.PW_BRIDGE_CORE_INFO_CB(self._core_info_cb)
        self._core_done_cb_c = pw.PW_BRIDGE_CORE_DONE_CB(self._core_done_cb)
        self._core_error_cb_c = pw.PW_BRIDGE_CORE_ERROR_CB(self._core_error_cb)
        self._node_info_cb_c = pw.PW_BRIDGE_NODE_INFO_CB(self._node_info_cb)
        self._node_param_cb_c = pw.PW_BRIDGE_NODE_PARAM_CB(
            self._node_param_cb)
        self._port_info_cb_c = pw.PW_BRIDGE_PORT_INFO_CB(self._port_info_cb)
        self._link_info_cb_c = pw.PW_BRIDGE_LINK_INFO_CB(self._link_info_cb)
        self._device_info_cb_c = pw.PW_BRIDGE_DEVICE_INFO_CB(
            self._device_info_cb)

        if pw.HAVE_METADATA:
            self._metadata_cb_c = pw.PW_BRIDGE_METADATA_PROPERTY_CB(
                self._metadata_property_cb)
        else:
            self._metadata_cb_c = None

        self._running = False
        self._core_lost = False
        self._loop_thread_id: Optional[int] = None

        self._objects_lock = threading.RLock()
        self.objects: dict[int, tuple[str, dict]] = {}
        self._node_listeners: dict[int, int] = {}
        self._node_proxies: dict[int, object] = {}
        self._port_listeners: dict[int, int] = {}
        self._port_proxies: dict[int, object] = {}
        self._link_listeners: dict[int, int] = {}
        self._link_proxies: dict[int, object] = {}
        self._device_listeners: dict[int, int] = {}
        self._device_proxies: dict[int, object] = {}

        self.metadata: dict[int, dict[int, dict[str, tuple[str, str]]]] = {}

        # Node params.
        self._node_params_available: dict[int, set] = {}
        self._node_params_cache: dict[int, dict] = {}
        self._node_param_waiters: dict[int, list] = {}
        # FIFO of in-flight enum_params requests. The server does not
        # echo our seq back; it uses its own counter. We correlate
        # responses with requests by arrival order, which PipeWire
        # guarantees for messages on the same object.
        # Each entry: {"node_id": int, "param_seen": bool}
        self._node_pending: collections.deque = collections.deque()
        self._node_seq_counter: int = 0
        self._sync_seq_counter: int = 0

        self._pending_link_proxies: list = []

        self._reconnect_thread: Optional[threading.Thread] = None
        self._reconnect_stop = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
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
        if self._thread_loop:
            pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            with self._objects_lock:
                for node_id in list(self._node_listeners.keys()):
                    self._unbind_node_locked(node_id)
                for port_id in list(self._port_listeners.keys()):
                    self._unbind_port_locked(port_id)
                for link_id in list(self._link_listeners.keys()):
                    self._unbind_link_locked(link_id)
                for device_id in list(self._device_listeners.keys()):
                    self._unbind_device_locked(device_id)
                for metadata_id in list(self._metadata_listeners.keys()):
                    self._unbind_metadata_locked(metadata_id)
                for proxy, listener in self._pending_link_proxies:
                    pw._lib_wrapper.pw_bridge_link_listener_free(listener)
                    pw.proxy_destroy(proxy)
                self._pending_link_proxies.clear()

            if self._core is not None:
                pw._lib_wrapper.pw_bridge_core_sync(
                    self._core, pw.PW_ID_CORE, 0)

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

        if self._thread_loop:
            try:
                pw._lib.pw_thread_loop_stop(self._thread_loop)
            except Exception:
                _logger.exception("Error stopping the thread loop")

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
            self._device_listeners.clear()
            self._device_proxies.clear()
            self._pending_link_proxies.clear()
            self._metadata_proxies.clear()
            self._metadata_listeners.clear()
            self.metadata.clear()
            self._release_all_node_waiters_locked()
        _logger.info("PipeWire registry stopped")

    def _reconnect_loop(self):
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
                    self._device_listeners.clear()
                    self._device_proxies.clear()
                    self._pending_link_proxies.clear()
                    self._metadata_proxies.clear()
                    self._metadata_listeners.clear()
                    self.metadata.clear()
                    self._release_all_node_waiters_locked()
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

    def _release_all_node_waiters_locked(self):
        for waiters in self._node_param_waiters.values():
            for ev in waiters:
                ev.set()
        self._node_param_waiters.clear()
        self._node_params_cache.clear()
        self._node_params_available.clear()
        self._node_pending.clear()

    # ------------------------------------------------------------------
    # Registry callbacks
    # ------------------------------------------------------------------

    def _registry_global_cb(self, user_data, id_, permissions,
                            type_, version, props):
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
        elif type_str == pw.PW_TYPE_INTERFACE_Device:
            self._bind_device(id_)
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

    def _registry_global_remove_cb(self, user_data, id_):
        with self._objects_lock:
            self.objects.pop(id_, None)
            self._unbind_node_locked(id_)
            self._unbind_port_locked(id_)
            self._unbind_link_locked(id_)
            self._unbind_device_locked(id_)
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
        """Called by PipeWire when the server has processed every
        message up to `seq`.

        We correlate with our in-flight requests by arrival order.
        The sync we send after enum_params completes the round-trip
        for the first pending request: whether or not a param event
        was seen, we release the waiter. If no param was seen, the
        cache for the node is set to an empty dict.
        """
        with self._objects_lock:
            if not self._node_pending:
                return
            entry = self._node_pending.popleft()
            node_id = entry["node_id"]
            seen = entry["param_seen"]
            if not seen:
                self._node_params_cache.setdefault(node_id, {})
            for ev in self._node_param_waiters.pop(node_id, []):
                ev.set()

    def _core_error_cb(self, user_data, id_, seq, res, message):
        msg_str = message.decode() if message else ""

        # "enum params id:2 failed" is a legitimate answer from the
        # server when a node does not expose SPA_PARAM_Props. We now
        # avoid most of these by checking the node's params list
        # before issuing enum_params, but a race is still possible
        # (params list not yet received). Log at debug level.
        if "enum params id:2" in msg_str:
            _logger.debug("Core: %s", msg_str)
            return

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
                    self._device_listeners.clear()
                    self._device_proxies.clear()
                    self._pending_link_proxies.clear()
                    self._metadata_proxies.clear()
                    self._metadata_listeners.clear()
                    self.metadata.clear()
                    self._release_all_node_waiters_locked()
                if self._on_core_lost is not None:
                    try:
                        self._on_core_lost()
                    except Exception:
                        _logger.exception("Error in on_core_lost")

    def _node_info_cb(self, user_data, node_id, max_in, max_out,
                      change_mask, state, error, props,
                      params_ptr, n_params):
        if not props:
            return
        props_dict = props.contents.to_dict()
        props_dict["_max_input_ports"] = max_in
        props_dict["_max_output_ports"] = max_out
        props_dict["_change_mask"] = change_mask
        props_dict["_state"] = state
        props_dict["_error"] = error.decode() if error else ""

        # Record the list of params the node supports.
        param_ids = pw.param_ids_from_info(params_ptr, n_params)
        props_dict["_params"] = param_ids

        with self._objects_lock:
            self._node_params_available[node_id] = set(param_ids)
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

    def _node_param_cb(self, user_data, node_id, seq, id_,
                       index, next_, pod):
        """Called when the server sends a param response.

        The server's seq does not match ours, so we correlate by
        arrival order: the response belongs to the first pending
        request that has not yet seen a param event.
        """
        if id_ != pw.SPA_PARAM_Props:
            return
        parsed = pw.pod_parse_props(pod) if pod else {}

        with self._objects_lock:
            # Find the first pending entry that has not yet seen a
            # param event.
            target = None
            for entry in self._node_pending:
                if not entry["param_seen"]:
                    target = entry["node_id"]
                    entry["param_seen"] = True
                    break

            if target is not None and parsed:
                bucket = self._node_params_cache.setdefault(target, {})
                bucket.update(parsed)

        if target is not None and parsed and self._on_node_params is not None:
            try:
                self._on_node_params(target, seq, parsed)
            except Exception:
                _logger.exception("Error in on_node_params")

    def _port_info_cb(self, user_data, port_id, direction, change_mask,
                      props):
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
                      in_node, in_port, state, error, props):
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

    def _device_info_cb(self, user_data, device_id, change_mask, props):
        if not props:
            return
        props_dict = props.contents.to_dict()
        props_dict["_change_mask"] = change_mask
        with self._objects_lock:
            existing = self.objects.get(device_id)
            if existing is not None:
                type_str, base_props = existing
                merged = dict(base_props)
                merged.update(props_dict)
                self.objects[device_id] = (type_str, merged)
        if self._on_device_info is not None:
            try:
                self._on_device_info(device_id, props_dict)
            except Exception:
                _logger.exception("Error in on_device_info")

    def _metadata_property_cb(self, user_data, metadata_id, subject,
                              key, type_, value):
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
    # Binding proxies
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
                proxy, self._node_info_cb_c,
                self._node_param_cb_c, None)
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

    def _bind_device(self, device_id: int):
        if self._registry is None:
            return
        with self._objects_lock:
            if device_id in self._device_proxies:
                return
            proxy = pw._lib_wrapper.pw_bridge_bind_device(
                self._registry, device_id)
            if not proxy:
                return
            listener = pw._lib_wrapper.pw_bridge_device_listener_new(
                proxy, self._device_info_cb_c, None)
            if not listener:
                pw.proxy_destroy(proxy)
                return
            self._device_proxies[device_id] = proxy
            self._device_listeners[device_id] = listener

    def _unbind_node_locked(self, node_id: int):
        listener = self._node_listeners.pop(node_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_node_listener_free(listener)
        proxy = self._node_proxies.pop(node_id, None)
        if proxy:
            pw.proxy_destroy(proxy)
        self._node_params_cache.pop(node_id, None)
        self._node_params_available.pop(node_id, None)
        # Remove any pending requests for this node.
        self._node_pending = collections.deque(
            e for e in self._node_pending if e["node_id"] != node_id)
        for ev in self._node_param_waiters.pop(node_id, []):
            ev.set()

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

    def _unbind_device_locked(self, device_id: int):
        listener = self._device_listeners.pop(device_id, None)
        if listener:
            pw._lib_wrapper.pw_bridge_device_listener_free(listener)
        proxy = self._device_proxies.pop(device_id, None)
        if proxy:
            pw.proxy_destroy(proxy)

    # ------------------------------------------------------------------
    # Metadata accessors
    # ------------------------------------------------------------------

    def get_metadata(self, subject: int, key: str):
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
        if not pw.HAVE_METADATA:
            return False
        if value is None:
            return self.set_metadata(subject, key,
                                     pw.SPA_TYPE_STRING_JSON, None)
        raw = json.dumps(value)
        return self.set_metadata(subject, key,
                                 pw.SPA_TYPE_STRING_JSON, raw)

    # ------------------------------------------------------------------
    # Node props (SPA_PARAM_Props)
    # ------------------------------------------------------------------

    def _next_node_seq(self) -> int:
        """Return an increasing integer to pass to enum_params.

        PipeWire will replace this seq with its own counter in the
        param event. We do not rely on it for correlation; it only
        needs to be a valid monotonically increasing integer.
        """
        with self._objects_lock:
            self._node_seq_counter += 1
            return self._node_seq_counter

    def _next_sync_seq(self) -> int:
        """Return an increasing integer to pass to core_sync.

        Same caveat as _next_node_seq: the server has its own
        counter. We use the FIFO of pending requests to correlate,
        not this seq.
        """
        with self._objects_lock:
            self._sync_seq_counter += 1
            return self._sync_seq_counter

    def get_node_params_available(self, node_id: int):
        """Return the set of param ids the node exposes, or None if
        node.info has not been received yet."""
        with self._objects_lock:
            avail = self._node_params_available.get(node_id)
            return set(avail) if avail is not None else None

    def request_node_props(self, node_id: int) -> bool:
        """Asynchronously request SPA_PARAM_Props for a node.

        Returns False immediately if the node is known not to
        support SPA_PARAM_Props (its params list is available and
        does not contain the id), which avoids a server-side error.
        """
        if not self._running or self._core_lost:
            return False
        proxy = self._node_proxies.get(node_id)
        if not proxy:
            return False

        with self._objects_lock:
            avail = self._node_params_available.get(node_id)
        if avail is not None and pw.SPA_PARAM_Props not in avail:
            _logger.debug(
                "Node %d does not expose SPA_PARAM_Props (params=%s)",
                node_id, sorted(avail))
            return False

        seq = self._next_node_seq()
        sync_seq = self._next_sync_seq()

        with self._objects_lock:
            self._node_pending.append(
                {"node_id": node_id, "param_seen": False})

        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            ret = pw._lib_wrapper.pw_bridge_node_enum_params(
                proxy, seq, pw.SPA_PARAM_Props, 0, 1, None)
            if ret >= 0:
                pw._lib_wrapper.pw_bridge_core_sync(
                    self._core, pw.PW_ID_CORE, sync_seq)
            else:
                # enum_params failed; remove the pending entry we
                # just added so the FIFO stays consistent.
                with self._objects_lock:
                    if self._node_pending and \
                            self._node_pending[-1]["node_id"] == node_id:
                        self._node_pending.pop()
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)
        return ret >= 0

    def get_node_props(self, node_id: int, timeout: float = 2.0) -> dict:
        if self._loop_thread_id is not None and \
                threading.get_ident() == self._loop_thread_id:
            raise RuntimeError(
                "get_node_props() cannot be called from the "
                "PipeWire loop thread; use request_node_props() "
                "instead")

        with self._objects_lock:
            if node_id not in self._node_proxies:
                raise KeyError(
                    f"Node {node_id} is not bound; unknown node or "
                    f"not a node object")

            cached = self._node_params_cache.get(node_id)
            if cached is not None:
                return dict(cached)

            avail = self._node_params_available.get(node_id)
            # If node.info arrived and the node does not expose Props,
            # return an empty dict immediately.
            if avail is not None and pw.SPA_PARAM_Props not in avail:
                return {}

        ev = threading.Event()
        with self._objects_lock:
            self._node_param_waiters.setdefault(node_id, []).append(ev)

        ok = self.request_node_props(node_id)
        if not ok:
            with self._objects_lock:
                waiters = self._node_param_waiters.get(node_id, [])
                if ev in waiters:
                    waiters.remove(ev)
            # The node was bound a moment ago, but the request could
            # not be sent (connection lost, or Props not available).
            return {}

        if not ev.wait(timeout):
            with self._objects_lock:
                waiters = self._node_param_waiters.get(node_id, [])
                if ev in waiters:
                    waiters.remove(ev)
            raise TimeoutError(
                f"No Props response for node {node_id} within "
                f"{timeout}s")

        with self._objects_lock:
            return dict(self._node_params_cache.get(node_id, {}))

    def set_node_volume(self, node_id: int, volume: float) -> bool:
        return self._set_node_props(node_id, volume=volume)

    def set_node_mute(self, node_id: int, mute: bool) -> bool:
        return self._set_node_props(node_id, mute=mute)

    def set_node_channel_volumes(self, node_id: int, volumes) -> bool:
        return self._set_node_props(
            node_id, channel_volumes=list(volumes))

    def _set_node_props(self, node_id: int, volume=None, mute=None,
                        channel_volumes=None) -> bool:
        if not self._running or self._core_lost:
            return False
        proxy = self._node_proxies.get(node_id)
        if not proxy:
            return False
        buffer, pod = pw.pod_build_props(
            volume=volume, mute=mute,
            channel_volumes=channel_volumes)
        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            ret = pw._lib_wrapper.pw_bridge_node_set_param(
                proxy, pw.SPA_PARAM_Props, 0, pod)
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)
        _ = buffer
        if ret >= 0:
            # Invalidate the cache: the next get_node_props() will
            # re-issue an enum_params and read the value back from
            # the server, so we stay in sync even if the server
            # clamped or adjusted the value.
            with self._objects_lock:
                self._node_params_cache.pop(node_id, None)
        return ret >= 0

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------

    def create_link(self, out_port_id: int, in_port_id: int) -> bool:
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