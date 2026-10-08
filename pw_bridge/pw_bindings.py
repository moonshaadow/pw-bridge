"""Minimal ctypes bindings for libpipewire-0.3.

Uses a C wrapper (libpw_bridge.so) for PipeWire functions that
are static inline and cannot be called directly by ctypes, or
that require per-instance user_data routing.

Rule:
  - if the symbol is exported by libpipewire-0.3.so.0, call it
    directly through `_lib`;
  - otherwise, it must be exposed by the C wrapper and called
    through `_lib_wrapper`.

Optional interfaces (metadata) are exposed conditionally. Check the
module-level HAVE_METADATA flag before using them.
"""

import ctypes
import ctypes.util
import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)


# --- Library loading --------------------------------------------------------

def _load_libpipewire() -> ctypes.CDLL:
    name = ctypes.util.find_library('pipewire-0.3')
    if name is None:
        raise ImportError(
            "libpipewire-0.3 not found. "
            "Please install pipewire and libpipewire-0.3-dev.")
    return ctypes.CDLL(name, use_errno=True)


def _load_wrapper() -> ctypes.CDLL:
    """Locate libpw_bridge.so.

    Search order:
      1. PW_BRIDGE_LIBRARY environment variable
      2. ctypes.util.find_library('pw_bridge')
      3. alongside the package (development checkout)
      4. one level up in a 'native' directory (development checkout)
    """
    env = os.environ.get("PW_BRIDGE_LIBRARY")
    if env:
        p = Path(env)
        if p.is_file():
            return ctypes.CDLL(str(p), use_errno=True)
        raise ImportError(
            f"PW_BRIDGE_LIBRARY points to a missing file: {env}")

    found = ctypes.util.find_library('pw_bridge')
    if found:
        try:
            return ctypes.CDLL(found, use_errno=True)
        except OSError:
            pass

    here = Path(__file__).resolve().parent
    candidates = [
        here / "libpw_bridge.so",
        here.parent / "native" / "libpw_bridge.so",
    ]
    for c in candidates:
        if c.is_file():
            return ctypes.CDLL(str(c), use_errno=True)

    raise ImportError(
        "C wrapper not found. Build it with: ./native/build.sh\n"
        f"Searched: {[str(c) for c in candidates]}")


_lib = _load_libpipewire()
_lib_wrapper = _load_wrapper()


# --- Constants --------------------------------------------------------------

PW_ID_CORE = 0
PW_ID_ANY  = 0xFFFFFFFF

# Port directions (enum pw_direction)
PW_DIRECTION_INPUT  = 0
PW_DIRECTION_OUTPUT = 1

# Node states (enum pw_node_state)
PW_NODE_STATE_ERROR     = -1
PW_NODE_STATE_CREATING  = 0
PW_NODE_STATE_SUSPENDED = 1
PW_NODE_STATE_IDLE      = 2
PW_NODE_STATE_RUNNING   = 3

# Link states (enum pw_link_state)
PW_LINK_STATE_ERROR        = -1
PW_LINK_STATE_CREATING     = 0
PW_LINK_STATE_ALLOCATING   = 1
PW_LINK_STATE_NEGOTIATING  = 2
PW_LINK_STATE_PAUSED       = 3
PW_LINK_STATE_ACTIVE       = 4

# Device change mask bits (enum pw_device_change_mask)
PW_DEVICE_CHANGE_MASK_PROPS  = 1 << 0
PW_DEVICE_CHANGE_MASK_PARAMS = 1 << 1

# Interface type strings
PW_TYPE_INTERFACE_Node     = "PipeWire:Interface:Node"
PW_TYPE_INTERFACE_Port     = "PipeWire:Interface:Port"
PW_TYPE_INTERFACE_Link     = "PipeWire:Interface:Link"
PW_TYPE_INTERFACE_Client   = "PipeWire:Interface:Client"
PW_TYPE_INTERFACE_Device   = "PipeWire:Interface:Device"
PW_TYPE_INTERFACE_Metadata = "PipeWire:Interface:Metadata"

# SPA metadata types
SPA_TYPE_STRING_JSON = "Spa:String:JSON"


# --- SPA structures ---------------------------------------------------------

class spa_list(ctypes.Structure):
    pass

spa_list._fields_ = [
    ("next", ctypes.POINTER(spa_list)),
    ("prev", ctypes.POINTER(spa_list)),
]


class spa_hook(ctypes.Structure):
    """Mirror of struct spa_hook.

    Only used as an opaque type. Never instantiate this from Python:
    the C wrapper allocates and frees the real hooks internally.
    """
    _fields_ = [
        ("link",    spa_list),
        ("cb",      spa_list),
        ("removed", spa_list),
        ("priv",    ctypes.c_void_p),
    ]


class spa_dict_item(ctypes.Structure):
    _fields_ = [
        ("key",   ctypes.c_char_p),
        ("value", ctypes.c_char_p),
    ]


class spa_dict(ctypes.Structure):
    _fields_ = [
        ("flags",   ctypes.c_uint32),
        ("n_items", ctypes.c_uint32),
        ("items",   ctypes.POINTER(spa_dict_item)),
    ]

    def to_dict(self) -> dict:
        result = {}
        if not self.items:
            return result
        for i in range(self.n_items):
            item = self.items[i]
            if item.key and item.value:
                try:
                    result[item.key.decode()] = item.value.decode()
                except UnicodeDecodeError:
                    continue
        return result


# --- PipeWire opaque structures ---------------------------------------------

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


# --- Callback prototypes (match pw_bridge.c) --------------------------------

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

PW_BRIDGE_CORE_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_uint32,
)

PW_BRIDGE_CORE_DONE_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_int,
)

PW_BRIDGE_CORE_ERROR_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_char_p,
)

PW_BRIDGE_NODE_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.POINTER(spa_dict),
)

PW_BRIDGE_PORT_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.POINTER(spa_dict),
)

PW_BRIDGE_LINK_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.POINTER(spa_dict),
)

PW_BRIDGE_DEVICE_INFO_CB = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,   # user_data
    ctypes.c_uint32,   # device_id
    ctypes.c_uint32,   # change_mask
    ctypes.POINTER(spa_dict),
)

PW_BRIDGE_METADATA_PROPERTY_CB = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
)


# --- libpipewire: init / main loop ------------------------------------------

_lib.pw_init.argtypes = [ctypes.POINTER(ctypes.c_int),
                         ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))]
_lib.pw_init.restype = None

_lib.pw_deinit.argtypes = []
_lib.pw_deinit.restype = None

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


# --- libpipewire: context ---------------------------------------------------

_lib.pw_context_new.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(spa_dict),
    ctypes.c_size_t,
]
_lib.pw_context_new.restype = ctypes.POINTER(pw_context)

_lib.pw_context_destroy.argtypes = [ctypes.POINTER(pw_context)]
_lib.pw_context_destroy.restype = None

_lib.pw_context_connect.argtypes = [
    ctypes.POINTER(pw_context),
    ctypes.POINTER(spa_dict),
    ctypes.c_size_t,
]
_lib.pw_context_connect.restype = ctypes.POINTER(pw_core)


# --- libpipewire: core ------------------------------------------------------

_lib.pw_core_disconnect.argtypes = [ctypes.POINTER(pw_core)]
_lib.pw_core_disconnect.restype = None


# --- libpipewire: thread loop -----------------------------------------------

_lib.pw_thread_loop_new.argtypes = [ctypes.c_char_p,
                                    ctypes.POINTER(spa_dict)]
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


# --- libpipewire: proxy (direct calls, symbol is exported) ------------------

_lib.pw_proxy_destroy.argtypes = [ctypes.POINTER(pw_proxy)]
_lib.pw_proxy_destroy.restype = None

_lib.pw_proxy_get_id.argtypes = [ctypes.POINTER(pw_proxy)]
_lib.pw_proxy_get_id.restype = ctypes.c_uint32

_lib.pw_proxy_get_bound_id.argtypes = [ctypes.POINTER(pw_proxy)]
_lib.pw_proxy_get_bound_id.restype = ctypes.c_uint32


# --- libpipewire: state helpers (direct calls, exported) --------------------

_lib.pw_node_state_as_string.argtypes = [ctypes.c_int]
_lib.pw_node_state_as_string.restype = ctypes.c_char_p

_lib.pw_link_state_as_string.argtypes = [ctypes.c_int]
_lib.pw_link_state_as_string.restype = ctypes.c_char_p


# --- C wrapper: version -----------------------------------------------------

for _name in (
    "pw_bridge_version_registry",
    "pw_bridge_version_core",
    "pw_bridge_version_link",
    "pw_bridge_version_node",
    "pw_bridge_version_port",
    "pw_bridge_version_client",
    "pw_bridge_version_device",
):
    _fn = getattr(_lib_wrapper, _name)
    _fn.argtypes = []
    _fn.restype = ctypes.c_uint32

PW_VERSION_REGISTRY = _lib_wrapper.pw_bridge_version_registry()
PW_VERSION_CORE     = _lib_wrapper.pw_bridge_version_core()
PW_VERSION_LINK     = _lib_wrapper.pw_bridge_version_link()
PW_VERSION_NODE     = _lib_wrapper.pw_bridge_version_node()
PW_VERSION_PORT     = _lib_wrapper.pw_bridge_version_port()
PW_VERSION_CLIENT   = _lib_wrapper.pw_bridge_version_client()
PW_VERSION_DEVICE   = _lib_wrapper.pw_bridge_version_device()


# --- C wrapper: registry ----------------------------------------------------

_lib_wrapper.pw_bridge_get_registry.argtypes = [ctypes.POINTER(pw_core)]
_lib_wrapper.pw_bridge_get_registry.restype = ctypes.POINTER(pw_registry)

_lib_wrapper.pw_bridge_registry_listener_new.argtypes = [
    ctypes.POINTER(pw_registry),
    PW_BRIDGE_GLOBAL_CB,
    PW_BRIDGE_GLOBAL_REMOVE_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_registry_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_registry_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_registry_listener_free.restype = None

_lib_wrapper.pw_bridge_registry_destroy.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_registry_destroy.restype = ctypes.c_int

_lib_wrapper.pw_bridge_destroy_link.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_destroy_link.restype = ctypes.c_int


# --- C wrapper: core listener -----------------------------------------------

_lib_wrapper.pw_bridge_core_listener_new.argtypes = [
    ctypes.POINTER(pw_core),
    PW_BRIDGE_CORE_INFO_CB,
    PW_BRIDGE_CORE_DONE_CB,
    PW_BRIDGE_CORE_ERROR_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_core_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_core_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_core_listener_free.restype = None

_lib_wrapper.pw_bridge_core_sync.argtypes = [
    ctypes.POINTER(pw_core),
    ctypes.c_uint32,
    ctypes.c_int,
]
_lib_wrapper.pw_bridge_core_sync.restype = ctypes.c_int


# --- C wrapper: node --------------------------------------------------------

_lib_wrapper.pw_bridge_bind_node.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_node.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_node_listener_new.argtypes = [
    ctypes.POINTER(pw_proxy),
    PW_BRIDGE_NODE_INFO_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_node_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_node_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_node_listener_free.restype = None


# --- C wrapper: port --------------------------------------------------------

_lib_wrapper.pw_bridge_bind_port.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_port.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_port_listener_new.argtypes = [
    ctypes.POINTER(pw_proxy),
    PW_BRIDGE_PORT_INFO_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_port_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_port_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_port_listener_free.restype = None


# --- C wrapper: link --------------------------------------------------------

_lib_wrapper.pw_bridge_bind_link.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_link.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_link_listener_new.argtypes = [
    ctypes.POINTER(pw_proxy),
    PW_BRIDGE_LINK_INFO_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_link_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_link_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_link_listener_free.restype = None

_lib_wrapper.pw_bridge_create_link.argtypes = [
    ctypes.POINTER(pw_core),
    ctypes.c_uint32,
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_create_link.restype = ctypes.POINTER(pw_proxy)


# --- C wrapper: client ------------------------------------------------------

_lib_wrapper.pw_bridge_bind_client.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_client.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_client_update_properties.argtypes = [
    ctypes.POINTER(pw_proxy),
    ctypes.POINTER(spa_dict),
]
_lib_wrapper.pw_bridge_client_update_properties.restype = ctypes.c_int


# --- C wrapper: device ------------------------------------------------------

_lib_wrapper.pw_bridge_bind_device.argtypes = [
    ctypes.POINTER(pw_registry),
    ctypes.c_uint32,
]
_lib_wrapper.pw_bridge_bind_device.restype = ctypes.POINTER(pw_proxy)

_lib_wrapper.pw_bridge_device_listener_new.argtypes = [
    ctypes.POINTER(pw_proxy),
    PW_BRIDGE_DEVICE_INFO_CB,
    ctypes.c_void_p,
]
_lib_wrapper.pw_bridge_device_listener_new.restype = ctypes.c_void_p

_lib_wrapper.pw_bridge_device_listener_free.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_device_listener_free.restype = None


# --- C wrapper: metadata (optional, PipeWire >= 1.2) ------------------------

HAVE_METADATA = False
PW_VERSION_METADATA = 0

try:
    _lib_wrapper.pw_bridge_version_metadata.argtypes = []
    _lib_wrapper.pw_bridge_version_metadata.restype = ctypes.c_uint32

    _lib_wrapper.pw_bridge_bind_metadata.argtypes = [
        ctypes.POINTER(pw_registry),
        ctypes.c_uint32,
    ]
    _lib_wrapper.pw_bridge_bind_metadata.restype = ctypes.POINTER(pw_proxy)

    _lib_wrapper.pw_bridge_metadata_listener_new.argtypes = [
        ctypes.POINTER(pw_proxy),
        PW_BRIDGE_METADATA_PROPERTY_CB,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    _lib_wrapper.pw_bridge_metadata_listener_new.restype = ctypes.c_void_p

    _lib_wrapper.pw_bridge_metadata_listener_free.argtypes = [ctypes.c_void_p]
    _lib_wrapper.pw_bridge_metadata_listener_free.restype = None

    _lib_wrapper.pw_bridge_metadata_set_property.argtypes = [
        ctypes.POINTER(pw_proxy),
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    _lib_wrapper.pw_bridge_metadata_set_property.restype = ctypes.c_int

    PW_VERSION_METADATA = _lib_wrapper.pw_bridge_version_metadata()
    HAVE_METADATA = True
except AttributeError:
    _logger.debug(
        "Metadata interface not available in this libpipewire version.")


# --- C wrapper: thread loop helpers -----------------------------------------

_lib_wrapper.pw_bridge_thread_loop_lock.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_thread_loop_lock.restype = None

_lib_wrapper.pw_bridge_thread_loop_unlock.argtypes = [ctypes.c_void_p]
_lib_wrapper.pw_bridge_thread_loop_unlock.restype = None


# --- Helpers ----------------------------------------------------------------

def proxy_destroy(proxy) -> None:
    """Destroy a proxy.

    Convenience wrapper around the exported pw_proxy_destroy, which
    is not in the C wrapper because it does not need to be.
    """
    if proxy:
        _lib.pw_proxy_destroy(proxy)


def state_name(state: int) -> str:
    """Return a human-readable node state name."""
    s = _lib.pw_node_state_as_string(state)
    return s.decode() if s else f"unknown({state})"


def link_state_name(state: int) -> str:
    """Return a human-readable link state name."""
    s = _lib.pw_link_state_as_string(state)
    return s.decode() if s else f"unknown({state})"


class SpaDictBuilder:
    """Build a C spa_dict from a Python dict.

    The returned object owns the underlying buffers. It must be kept
    alive for the whole duration of the C call that uses the dict.
    """

    def __init__(self, data: dict):
        self._refs: list = []
        n = len(data)
        items_array = (spa_dict_item * n)()

        for i, (key, value) in enumerate(data.items()):
            key_b = key.encode("utf-8")
            value_b = value.encode("utf-8")
            self._refs.append(key_b)
            self._refs.append(value_b)
            items_array[i] = spa_dict_item(key_b, value_b)

        self._refs.append(items_array)
        self._items_array = items_array
        self.dict = spa_dict(
            flags=0,
            n_items=n,
            items=ctypes.cast(items_array,
                              ctypes.POINTER(spa_dict_item)),
        )

    def __enter__(self):
        return self.dict

    def __exit__(self, exc_type, exc, tb):
        return False


def spa_dict_from_python(data: dict) -> SpaDictBuilder:
    """Convenience wrapper. Usage:

        builder = spa_dict_from_python({"key": "value"})
        ret = _lib_wrapper.pw_bridge_client_update_properties(
            proxy, ctypes.byref(builder.dict))
    """
    return SpaDictBuilder(data)