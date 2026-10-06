"""Minimal ctypes bindings for libpipewire-0.3.

Uses a C wrapper (libpw_bridge.so) for PipeWire functions that
are static inline and cannot be called directly by ctypes.
"""

import ctypes
import ctypes.util
import logging
from pathlib import Path

_logger = logging.getLogger(__name__)


# --- Library loading --------------------------------------------------------

_lib_name = ctypes.util.find_library('pipewire-0.3')
if _lib_name is None:
    raise ImportError(
        "libpipewire-0.3 not found. "
        "Please install pipewire and libpipewire-0.3-dev.")

_lib = ctypes.CDLL(_lib_name, use_errno=True)

_wrapper_path = Path(__file__).parent.parent / "native" / "libpw_bridge.so"
if not _wrapper_path.is_file():
    raise ImportError(
        f"C wrapper not found: {_wrapper_path}\n"
        f"Build it with: ./native/build.sh")

_lib_wrapper = ctypes.CDLL(str(_wrapper_path), use_errno=True)


# --- Constants --------------------------------------------------------------

PW_VERSION_REGISTRY = 3
PW_VERSION_CORE = 3
PW_VERSION_LINK = 3
PW_ID_CORE = 0

PW_TYPE_INTERFACE_Node = "PipeWire:Interface:Node"
PW_TYPE_INTERFACE_Port = "PipeWire:Interface:Port"
PW_TYPE_INTERFACE_Link = "PipeWire:Interface:Link"
PW_TYPE_INTERFACE_Client = "PipeWire:Interface:Client"
PW_TYPE_INTERFACE_Device = "PipeWire:Interface:Device"
PW_TYPE_INTERFACE_Metadata = "PipeWire:Interface:Metadata"


# --- SPA structures ---------------------------------------------------------

class spa_hook(ctypes.Structure):
    _fields_ = [
        ("link", ctypes.c_void_p),
        ("cb", ctypes.c_void_p),
        ("removed", ctypes.c_void_p),
        ("priv", ctypes.c_void_p),
    ]


class spa_dict_item(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_char_p),
        ("value", ctypes.c_char_p),
    ]


class spa_dict(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("n_items", ctypes.c_uint32),
        ("items", ctypes.POINTER(spa_dict_item)),
    ]

    def to_dict(self) -> dict:
        result = {}
        if not self.items:
            return result
        for i in range(self.n_items):
            item = self.items[i]
            if item.key and item.value:
                result[item.key.decode()] = item.value.decode()
        return result


class spa_source(ctypes.Structure):
    pass


# --- PipeWire structures ----------------------------------------------------

class pw_main_loop(ctypes.Structure):
    pass


class pw_context(ctypes.Structure):
    pass


class pw_core(ctypes.Structure):
    pass


class pw_registry(ctypes.Structure):
    pass


class pw_proxy(ctypes.Structure):
    pass


# --- Callbacks --------------------------------------------------------------

REGISTRY_GLOBAL_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.POINTER(spa_dict)
)

REGISTRY_GLOBAL_REMOVE_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32
)


class pw_registry_events(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("global_cb", REGISTRY_GLOBAL_CB),
        ("global_remove_cb", REGISTRY_GLOBAL_REMOVE_CB),
    ]


# --- libpipewire signatures: main loop --------------------------------------

_lib.pw_init.argtypes = [ctypes.POINTER(ctypes.c_int),
                         ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))]
_lib.pw_init.restype = None

_lib.pw_main_loop_new.argtypes = [ctypes.POINTER(spa_dict)]
_lib.pw_main_loop_new.restype = ctypes.POINTER(pw_main_loop)

_lib.pw_main_loop_destroy.argtypes = [ctypes.POINTER(pw_main_loop)]
_lib.pw_main_loop_destroy.restype = None

_lib.pw_main_loop_run.argtypes = [ctypes.POINTER(pw_main_loop)]
_lib.pw_main_loop_run.restype = ctypes.c_int

_lib.pw_main_loop_quit.argtypes = [ctypes.POINTER(pw_main_loop)]
_lib.pw_main_loop_quit.restype = None

_lib.pw_main_loop_get_loop.argtypes = [ctypes.POINTER(pw_main_loop)]
_lib.pw_main_loop_get_loop.restype = ctypes.c_void_p


# --- libpipewire signatures: context ----------------------------------------

_lib.pw_context_new.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(spa_dict),
    ctypes.c_size_t
]
_lib.pw_context_new.restype = ctypes.POINTER(pw_context)

_lib.pw_context_destroy.argtypes = [ctypes.POINTER(pw_context)]
_lib.pw_context_destroy.restype = None

_lib.pw_context_connect.argtypes = [
    ctypes.POINTER(pw_context),
    ctypes.POINTER(spa_dict),
    ctypes.c_size_t
]
_lib.pw_context_connect.restype = ctypes.POINTER(pw_core)


# --- libpipewire signatures: core -------------------------------------------

_lib.pw_core_disconnect.argtypes = [ctypes.POINTER(pw_core)]
_lib.pw_core_disconnect.restype = None


# --- libpipewire signatures: proxy ------------------------------------------

_lib.pw_proxy_destroy.argtypes = [ctypes.POINTER(pw_proxy)]
_lib.pw_proxy_destroy.restype = None


# --- libpipewire signatures: thread loop ------------------------------------

_lib.pw_thread_loop_new.argtypes = [
    ctypes.c_char_p,
    ctypes.POINTER(spa_dict),
]
_lib.pw_thread_loop_new.restype = ctypes.c_void_p

_lib.pw_thread_loop_destroy.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_destroy.restype = None

_lib.pw_thread_loop_start.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_start.restype = ctypes.c_int

_lib.pw_thread_loop_stop.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_stop.restype = None

_lib.pw_thread_loop_get_loop.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_get_loop.restype = ctypes.c_void_p

_lib.pw_thread_loop_lock.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_lock.restype = None

_lib.pw_thread_loop_unlock.argtypes = [ctypes.c_void_p]
_lib.pw_thread_loop_unlock.restype = None


# --- C wrapper signatures: registry -----------------------------------------

_lib_wrapper.pw_bridge_get_registry.argtypes = [ctypes.POINTER(pw_core)]
_lib_wrapper.pw_bridge_get_registry.restype = ctypes.POINTER(pw_registry)

PW_BRIDGE_GLOBAL_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_uint32,
    ctypes.POINTER(spa_dict),
)

PW_BRIDGE_GLOBAL_REMOVE_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
)

_lib_wrapper.pw_bridge_set_registry_callbacks.argtypes = [
    PW_BRIDGE_GLOBAL_CB,
    PW_BRIDGE_GLOBAL_REMOVE_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_set_registry_callbacks.restype = None

_lib_wrapper.pw_bridge_registry_add_listener.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.POINTER(spa_hook),
]
_lib_wrapper.pw_bridge_registry_add_listener.restype = ctypes.c_int


# --- C wrapper signatures: node binding -------------------------------------

PW_BRIDGE_NODE_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(spa_dict),
)

_lib_wrapper.pw_bridge_set_node_info_callback.argtypes = [
    PW_BRIDGE_NODE_INFO_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_set_node_info_callback.restype = None

_lib_wrapper.pw_bridge_bind_node.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_node.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_node_add_listener.argtypes = [
    ctypes.POINTER(pw_proxy),
]
_lib_wrapper.pw_bridge_node_add_listener.restype = ctypes.POINTER(spa_hook)


# --- C wrapper signatures: core ---------------------------------------------

PW_BRIDGE_CORE_ERROR_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,    # user_data
    ctypes.c_uint32,    # id
    ctypes.c_int,       # seq
    ctypes.c_int,       # res
    ctypes.c_char_p,    # message
)

_lib_wrapper.pw_bridge_set_core_error_callback.argtypes = [
    PW_BRIDGE_CORE_ERROR_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_set_core_error_callback.restype = None

_lib_wrapper.pw_bridge_add_core_listener.argtypes = [ctypes.POINTER(pw_core)]
_lib_wrapper.pw_bridge_add_core_listener.restype = None


# --- C wrapper signatures: link ---------------------------------------------

_lib_wrapper.pw_bridge_create_link.argtypes = [
    ctypes.POINTER(pw_core),
    ctypes.c_uint32,
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_create_link.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_destroy_link.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_destroy_link.restype = ctypes.c_int

_lib_wrapper.pw_bridge_destroy_proxy.argtypes = [ctypes.POINTER(pw_proxy)]
_lib_wrapper.pw_bridge_destroy_proxy.restype = None


# --- C wrapper signatures: thread loop helpers ------------------------------

_lib_wrapper.pw_bridge_thread_loop_lock.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_thread_loop_lock.restype = None

_lib_wrapper.pw_bridge_thread_loop_unlock.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_thread_loop_unlock.restype = None


# --- Helpers ----------------------------------------------------------------

def spa_dict_from_python(data: dict) -> tuple:
    """Build a C spa_dict from a Python dict."""
    refs = []
    items_array = (spa_dict_item * len(data))()

    for i, (key, value) in enumerate(data.items()):
        key_b = key.encode()
        value_b = value.encode()
        refs.append(key_b)
        refs.append(value_b)
        items_array[i] = spa_dict_item(key_b, value_b)

    d = spa_dict(
        flags=0,
        n_items=len(data),
        items=ctypes.cast(items_array, ctypes.POINTER(spa_dict_item))
    )
    refs.append(items_array)
    return d, refs