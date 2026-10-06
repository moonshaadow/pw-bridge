"""PipeWire connection management and registry subscription.

Uses pw_thread_loop to ensure that all PipeWire API calls are
made from the loop's thread.

This module also handles:
- binding proxies on nodes to retrieve their complete properties
  (node.group, node.link-group, factory.name, etc.)
- detection of core loss (PipeWire restart) via the `core error`
  and `global_remove(id=0)` events
- automatic reconnection.
"""

import ctypes
import logging
import threading
import time
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
            on_core_lost: Optional[Callable[[], None]] = None,
            on_core_restored: Optional[Callable[[], None]] = None):
        self._on_global_added = on_global_added
        self._on_global_removed = on_global_removed
        self._on_node_info = on_node_info
        self._on_core_lost = on_core_lost
        self._on_core_restored = on_core_restored

        self._thread_loop = None
        self._context = None
        self._core = None
        self._registry = None

        # Strong references to C callbacks
        self._registry_hook = pw.spa_hook()
        self._global_cb_c = pw.PW_BRIDGE_GLOBAL_CB(self._registry_global_cb)
        self._global_remove_cb_c = pw.PW_BRIDGE_GLOBAL_REMOVE_CB(
            self._registry_global_remove_cb)
        self._node_info_cb_c = pw.PW_BRIDGE_NODE_INFO_CB(self._node_info_cb)
        self._core_error_cb_c = pw.PW_BRIDGE_CORE_ERROR_CB(
            self._core_error_cb)

        pw._lib_wrapper.pw_bridge_set_registry_callbacks(
            self._global_cb_c, self._global_remove_cb_c, None)
        pw._lib_wrapper.pw_bridge_set_node_info_callback(
            self._node_info_cb_c, None)
        pw._lib_wrapper.pw_bridge_set_core_error_callback(
            self._core_error_cb_c, None)

        self._running = False
        self._core_lost = False

        self.objects: dict[int, tuple[str, dict]] = {}
        self._node_hooks: dict[int, object] = {}
        self._node_proxies: dict[int, object] = {}

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
            raise RuntimeError(
                "Initial PipeWire connection failed")

        self._running = True
        self._core_lost = False

        # Start the monitoring/reconnection thread
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="PipeWireReconnect",
            daemon=True)
        self._reconnect_thread.start()

        _logger.info("PipeWire registry started")

    def _create_connection(self) -> bool:
        """Create the thread loop, context, core and registry."""
        self._thread_loop = pw._lib.pw_thread_loop_new(b"pw_bridge", None)
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

        pw._lib_wrapper.pw_bridge_add_core_listener(self._core)

        self._registry = pw._lib_wrapper.pw_bridge_get_registry(self._core)
        if not self._registry:
            _logger.error("Failed to retrieve the registry")
            return False

        ret = pw._lib_wrapper.pw_bridge_registry_add_listener(
            self._registry, ctypes.byref(self._registry_hook))
        if ret < 0:
            _logger.error("Failed to register the registry listener")
            return False

        ret = pw._lib.pw_thread_loop_start(self._thread_loop)
        if ret < 0:
            _logger.error("Failed to start the thread loop")
            return False

        return True

    def _destroy_connection(self):
        """Cleanly destroy the PipeWire connection."""
        if self._thread_loop:
            try:
                pw._lib.pw_thread_loop_stop(self._thread_loop)
            except Exception:
                pass

        if self._core:
            try:
                pw._lib.pw_core_disconnect(self._core)
            except Exception:
                pass
            self._core = None

        if self._context:
            try:
                pw._lib.pw_context_destroy(self._context)
            except Exception:
                pass
            self._context = None

        if self._thread_loop:
            try:
                pw._lib.pw_thread_loop_destroy(self._thread_loop)
            except Exception:
                pass
            self._thread_loop = None

        self._registry = None

    def stop(self):
        """Cleanly stop the connection and the reconnection thread."""
        if not self._running:
            return

        self._running = False
        self._reconnect_stop.set()

        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=2.0)
            self._reconnect_thread = None

        self._destroy_connection()

        self.objects.clear()
        self._node_hooks.clear()
        self._node_proxies.clear()

        _logger.info("PipeWire registry stopped")

    # ------------------------------------------------------------------
    # Reconnection thread
    # ------------------------------------------------------------------

    def _reconnect_loop(self):
        """Monitor the connection and attempt to reconnect."""
        while not self._reconnect_stop.is_set():
            if self._core_lost:
                _logger.info("Attempting to reconnect to PipeWire...")

                # Reset hooks and callbacks
                self._registry_hook = pw.spa_hook()
                self._global_cb_c = pw.PW_BRIDGE_GLOBAL_CB(
                    self._registry_global_cb)
                self._global_remove_cb_c = pw.PW_BRIDGE_GLOBAL_REMOVE_CB(
                    self._registry_global_remove_cb)
                self._node_info_cb_c = pw.PW_BRIDGE_NODE_INFO_CB(
                    self._node_info_cb)
                self._core_error_cb_c = pw.PW_BRIDGE_CORE_ERROR_CB(
                    self._core_error_cb)

                pw._lib_wrapper.pw_bridge_set_registry_callbacks(
                    self._global_cb_c, self._global_remove_cb_c, None)
                pw._lib_wrapper.pw_bridge_set_node_info_callback(
                    self._node_info_cb_c, None)
                pw._lib_wrapper.pw_bridge_set_core_error_callback(
                    self._core_error_cb_c, None)

                self._destroy_connection()
                self.objects.clear()
                self._node_hooks.clear()
                self._node_proxies.clear()

                if self._create_connection():
                    _logger.info("Reconnection to PipeWire succeeded")
                    self._core_lost = False

                    if self._on_core_restored is not None:
                        try:
                            self._on_core_restored()
                        except Exception:
                            _logger.exception(
                                "Error in on_core_restored")
                else:
                    _logger.info(
                        "Failed, retrying in 1 second")

            time.sleep(1.0)

    # ------------------------------------------------------------------
    # Registry callbacks
    # ------------------------------------------------------------------

    def _registry_global_cb(
            self, user_data, id_: int, permissions: int,
            type_: bytes, version: int, props):
        type_str = type_.decode() if type_ else ""
        props_dict = props.contents.to_dict() if props else {}

        self.objects[id_] = (type_str, props_dict)

        if type_str == pw.PW_TYPE_INTERFACE_Node:
            self._bind_node(id_)

        try:
            self._on_global_added(id_, type_str, props_dict)
        except Exception:
            _logger.exception("Error in on_global_added")

    def _registry_global_remove_cb(self, user_data, id_: int):
        self.objects.pop(id_, None)
        self._node_hooks.pop(id_, None)
        self._node_proxies.pop(id_, None)

        # If it's the core itself that disappeared, then the PipeWire
        # server has restarted or stopped.
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

    def _core_error_cb(self, user_data, id_, seq, res, message):
        """Called when the core reports an error."""
        msg_str = message.decode() if message else ""
        _logger.warning(
            f"Core error: id={id_} seq={seq} res={res} msg={msg_str}")

        # res = -32 (EPIPE): connection lost
        # id = PW_ID_CORE: error on the core itself
        if id_ == pw.PW_ID_CORE or res == -32:
            if not self._core_lost:
                _logger.warning(
                    "Connection loss detected via core error")
                self._core_lost = True
                self.objects.clear()
                self._node_hooks.clear()
                self._node_proxies.clear()

                if self._on_core_lost is not None:
                    try:
                        self._on_core_lost()
                    except Exception:
                        _logger.exception("Error in on_core_lost")

    def _node_info_cb(self, user_data, node_id: int,
                      props: ctypes.POINTER(pw.spa_dict)):
        """Called when a node's complete properties are received."""
        if not props:
            return

        props_dict = props.contents.to_dict()

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

    # ------------------------------------------------------------------
    # Binding proxies on nodes
    # ------------------------------------------------------------------

    def _bind_node(self, node_id: int):
        if self._registry is None:
            return
        if node_id in self._node_proxies:
            return

        proxy = pw._lib_wrapper.pw_bridge_bind_node(
            self._registry, node_id)

        if not proxy:
            return

        hook = pw._lib_wrapper.pw_bridge_node_add_listener(proxy)
        if not hook:
            return

        self._node_proxies[node_id] = proxy
        self._node_hooks[node_id] = hook

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
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

        if not proxy:
            return False
        return True

    def destroy_link(self, link_id: int) -> bool:
        if not self._running or self._registry is None or self._core_lost:
            return False

        pw._lib_wrapper.pw_bridge_thread_loop_lock(self._thread_loop)
        try:
            ret = pw._lib_wrapper.pw_bridge_destroy_link(
                self._registry, link_id)
        finally:
            pw._lib_wrapper.pw_bridge_thread_loop_unlock(self._thread_loop)

        if ret < 0:
            return False
        return True

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def find_object(self, type_: str) -> dict[int, dict]:
        return {
            id_: props
            for id_, (t, props) in self.objects.items()
            if t == type_
        }

    def get_object(self, id_: int) -> Optional[tuple[str, dict]]:
        return self.objects.get(id_)