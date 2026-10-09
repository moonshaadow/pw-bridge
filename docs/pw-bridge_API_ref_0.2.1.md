# pw-bridge — API reference (0.2.1)

**Version:** 0.2.1 develop
**License:** GPL-2.0-or-later
**Targets:** libpipewire-0.3 (>= 0.3.30)

This document is the authoritative reference for the Python API of
`pw-bridge`. It describes what the package exposes, the exact
signatures of every callback, the ownership rules for every object,
and the conventions that must be followed by consumers (Patchanka,
PipeWire Control Center, or any other application).

---
## Changes in 0.2.1

New in this release:

- **Device interface** (§6.7): bind devices, listen to their
  `info` events. The registry binds every Device announced by
  the registry and exposes them through
  `find_object(pw.PW_TYPE_INTERFACE_Device)` and the
  `on_device_info` callback.

- **Node props** (§6.9): read and write SPA_PARAM_Props on
  nodes. Exposes `get_node_props`, `set_node_volume`,
  `set_node_mute`, `set_node_channel_volumes`, and the
  asynchronous `request_node_props`. Also
  `get_node_params_available` to inspect which params a node
  exposes.

- **Params list on node.info** (§7.3): the `on_node_info`
  callback now receives a `_params` key in its props dict, a
  list of integer param ids.

- **Metadata fixes**: `get_metadata_json` accepts the empty SPA
  type in addition to `Spa:String:JSON`, so server-published
  keys like `clock.rate` decode correctly.

- **New pitfall documented** (§11.6): the server does not echo
  the seq passed to enum_params. See the new pitfall section.

---

## 1. Design principles

Three rules drive the design of this package.

### 1.1 Rule of the C wrapper

A PipeWire function is exposed by the C wrapper
(`libpw_bridge.so`) **if and only if** it is declared `static inline`
in the PipeWire headers and therefore not exported by
`libpipewire-0.3.so.0`.

If the function is exported by the shared library, it is called
directly through ctypes (`pw._lib.<function>`). The wrapper does not
duplicate it.

Verification command:

```bash
nm -D /usr/lib/x86_64-linux-gnu/libpipewire-0.3.so.0 \
    | grep -E ' T ' | grep -E ' pw_' | sort
```

Anything starting with `pw_` in that list is callable directly.

### 1.2 Rule of per-instance listeners

Every listener (`registry`, `core`, `node`, `port`, `link`, `metadata` and `device`) stores its own `user_data` and its own
callbacks in the C wrapper. There are **no global callback variables**
anymore. This allows several `PipeWireRegistry` instances to coexist
in the same process without interfering with each other.

The `user_data` is currently unused (always `None`), but the plumbing
is in place so that a future feature can route per-proxy context
without changing the C ABI.

### 1.3 Rule of the GIL

Every C relay callback that invokes a Python callback acquires the
GIL with `PyGILState_Ensure()` before the call and releases it with
`PyGILState_Release()` after. This is mandatory because the callbacks
are invoked from the PipeWire thread loop, which is not a Python
thread.

**Python callbacks must not acquire the GIL themselves** (no `with
self._lock` on the GIL, no `PyGILState_Ensure` equivalent). ctypes
already ensures the GIL is held during the call.

---

## 2. Package layout

```
    pw-bridge/
    ├── native/
    │   ├── pw_bridge.c        C wrapper (static-inline PipeWire functions)
    │   ├── Makefile
    │   └── build.sh           Convenience build script
    ├── pw_bridge/
    │   ├── __init__.py        Package entry point
    │   ├── pw_bindings.py     ctypes bindings (direct + wrapper)
    │   └── registry.py        High-level PipeWireRegistry class
    ├── tests/
    │   ├── test_two_registries.py   Instance isolation, link roundtrip
    │   ├── test_metadata.py         Metadata read/write, JSON decode
    │   ├── test_device.py           Device info, listener lifecycle
    │   └── test_node_props.py       Volume, mute, channel volumes
    ├── docs/
    │   ├── pw-bridge_API_ref_0.2.md
    │   └── pw-bridge_API_ref_0.2.1.md
    ├── pyproject.toml
    ├── setup.py
    └── README.md

```

`__init__.py` exposes:

```python
from . import pw_bindings
from .registry import PipeWireRegistry

__all__ = ['pw_bindings', 'PipeWireRegistry']
```

Note: the module is named `registry.py`, not `pipewire_registry.py`.
Any reference to `pipewire_registry` in older documentation is
obsolete.

---

## 3. Building the C wrapper

```bash
cd native
./build.sh              # or: make
```

This produces `native/libpw_bridge.so`. The Python package searches
for it in this order:

1. `$PW_BRIDGE_LIBRARY` environment variable
2. `ctypes.util.find_library('pw_bridge')`
3. `pw_bridge/libpw_bridge.so` (installed package)
4. `native/libpw_bridge.so` (development checkout)

Requirements:

- `libpipewire-0.3-dev` (headers and pkg-config file)
- Python 3.8+ with `sysconfig` available
- A C compiler (`cc` by default)

The Makefile links against `libpipewire-0.3` and includes the Python
headers. It does **not** link against `libpython`: the Python symbols
(`PyGILState_Ensure`, `PyGILState_Release`) are resolved at load time
by the interpreter that loads the `.so`.

---

## 4. Constants

### 4.1 Special object IDs

```python
pw.PW_ID_CORE = 0
pw.PW_ID_ANY  = 0xFFFFFFFF
```

`PW_ID_CORE` is the ID of the core object itself. It is used to
detect a lost connection (`global_remove` with `id=0`, or `core
error` with `id=0`).

### 4.2 Port direction

```python
pw.PW_DIRECTION_INPUT  = 0
pw.PW_DIRECTION_OUTPUT = 1
```

### 4.3 Node state

```python
pw.PW_NODE_STATE_ERROR     = -1
pw.PW_NODE_STATE_CREATING  = 0
pw.PW_NODE_STATE_SUSPENDED = 1
pw.PW_NODE_STATE_IDLE      = 2
pw.PW_NODE_STATE_RUNNING   = 3
```

Use `pw.state_name(state)` to get a human-readable string.

### 4.4 Link state

```python
pw.PW_LINK_STATE_ERROR        = -1
pw.PW_LINK_STATE_CREATING     = 0
pw.PW_LINK_STATE_ALLOCATING   = 1
pw.PW_LINK_STATE_NEGOTIATING  = 2
pw.PW_LINK_STATE_PAUSED       = 3
pw.PW_LINK_STATE_ACTIVE       = 4
```

Use `pw.link_state_name(state)` to get a human-readable string.

### 4.5 Interface type strings

```python
pw.PW_TYPE_INTERFACE_Node     = "PipeWire:Interface:Node"
pw.PW_TYPE_INTERFACE_Port     = "PipeWire:Interface:Port"
pw.PW_TYPE_INTERFACE_Link     = "PipeWire:Interface:Link"
pw.PW_TYPE_INTERFACE_Client   = "PipeWire:Interface:Client"
pw.PW_TYPE_INTERFACE_Device   = "PipeWire:Interface:Device"
pw.PW_TYPE_INTERFACE_Metadata = "PipeWire:Interface:Metadata"
```

### 4.6 Interface versions

Read at import time from the C wrapper, so they always match the
installed libpipewire:

```python
pw.PW_VERSION_REGISTRY
pw.PW_VERSION_CORE
pw.PW_VERSION_LINK
pw.PW_VERSION_NODE
pw.PW_VERSION_PORT
pw.PW_VERSION_CLIENT
pw.PW_VERSION_DEVICE
```

---

## 5. Direct ctypes calls (`pw._lib`)

These functions are exported by `libpipewire-0.3.so.0` and are
declared in `pw_bindings.py` for direct use. They are **not** in the
C wrapper.

### 5.1 Initialization

```python
pw._lib.pw_init(None, None)          # global init, call once per process
pw._lib.pw_deinit()                  # optional, call once at exit
```

`pw_init` is reference-counted. Calling it from several
`PipeWireRegistry` instances in the same process is safe, but the
matching `pw_deinit` should be called only once, when the last
registry is gone.

### 5.2 Thread loop

```python
pw._lib.pw_thread_loop_new(name: bytes, props) -> void*
pw._lib.pw_thread_loop_start(loop: void*) -> int
pw._lib.pw_thread_loop_stop(loop: void*) -> None
pw._lib.pw_thread_loop_destroy(loop: void*) -> None
pw._lib.pw_thread_loop_get_loop(loop: void*) -> void*
pw._lib.pw_thread_loop_lock(loop: void*) -> None
pw._lib.pw_thread_loop_unlock(loop: void*) -> None
```

`pw_thread_loop_lock` is **recursive**. Taking it from a context that
already holds it (for example, a PipeWire callback) is safe.

### 5.3 Context

```python
pw._lib.pw_context_new(loop: void*, props, size) -> pw_context*
pw._lib.pw_context_connect(context, props, size) -> pw_core*
pw._lib.pw_context_destroy(context) -> None
```

### 5.4 Core

```python
pw._lib.pw_core_disconnect(core) -> None
```

### 5.5 Proxy

```python
pw._lib.pw_proxy_destroy(proxy) -> None
pw._lib.pw_proxy_get_id(proxy) -> int
pw._lib.pw_proxy_get_bound_id(proxy) -> int
```

Prefer the helper:

```python
pw.proxy_destroy(proxy)              # NULL-safe
```

### 5.6 State name helpers

```python
pw._lib.pw_node_state_as_string(state: int) -> bytes
pw._lib.pw_link_state_as_string(state: int) -> bytes
```

Prefer the helpers:

```python
pw.state_name(state)      -> str
pw.link_state_name(state) -> str
```

---

## 6. C wrapper calls (`pw._lib_wrapper`)

These functions wrap static-inline PipeWire functions and per-instance
listeners. They are the only reason the C wrapper exists.

### 6.1 Registry

```python
pw._lib_wrapper.pw_bridge_get_registry(core) -> pw_registry*
```

Wraps `pw_core_get_registry(core, PW_VERSION_REGISTRY, 0)`.

### 6.2 Thread loop helpers

```python
pw._lib_wrapper.pw_bridge_thread_loop_lock(loop: void*) -> None
pw._lib_wrapper.pw_bridge_thread_loop_unlock(loop: void*) -> None
```

Wraps `pw_thread_loop_lock` / `pw_thread_loop_unlock`, NULL-safe.

### 6.3 Registry listener

```python
pw._lib_wrapper.pw_bridge_registry_listener_new(
    registry,
    global_cb,          # PW_BRIDGE_GLOBAL_CB
    global_remove_cb,   # PW_BRIDGE_GLOBAL_REMOVE_CB
    user_data,          # c_void_p, currently always None
) -> int                # opaque handle, 0 on failure

pw._lib_wrapper.pw_bridge_registry_listener_free(handle) -> None
```

### 6.4 Core listener

```python
pw._lib_wrapper.pw_bridge_core_listener_new(
    core,
    info_cb,            # PW_BRIDGE_CORE_INFO_CB
    done_cb,            # PW_BRIDGE_CORE_DONE_CB
    error_cb,           # PW_BRIDGE_CORE_ERROR_CB
    user_data,
) -> int

pw._lib_wrapper.pw_bridge_core_listener_free(handle) -> None
```

### 6.5 Node

```python
pw._lib_wrapper.pw_bridge_bind_node(registry, node_id) -> pw_proxy*

pw._lib_wrapper.pw_bridge_node_listener_new(
    proxy,
    info_cb,            # PW_BRIDGE_NODE_INFO_CB
    user_data,
) -> int

pw._lib_wrapper.pw_bridge_node_listener_free(handle) -> None
```

### 6.6 Port

```python
pw._lib_wrapper.pw_bridge_bind_port(registry, port_id) -> pw_proxy*

pw._lib_wrapper.pw_bridge_port_listener_new(
    proxy,
    info_cb,            # PW_BRIDGE_PORT_INFO_CB
    user_data,
) -> int

pw._lib_wrapper.pw_bridge_port_listener_free(handle) -> None
```

### 6.7 Link

```python
pw._lib_wrapper.pw_bridge_bind_link(registry, link_id) -> pw_proxy*

pw._lib_wrapper.pw_bridge_link_listener_new(
    proxy,
    info_cb,            # PW_BRIDGE_LINK_INFO_CB
    user_data,
) -> int

pw._lib_wrapper.pw_bridge_link_listener_free(handle) -> None

pw._lib_wrapper.pw_bridge_create_link(core, out_port_id, in_port_id)
    -> pw_proxy*
```

`pw_bridge_create_link` wraps `pw_core_create_object` with the
`link-factory` and the two properties `link.output.port` and
`link.input.port`.

**Calling convention:** must be called under
`pw_bridge_thread_loop_lock`. In practice, `PipeWireRegistry.create_link`
takes care of this.

### 6.8 Client

```python
pw._lib_wrapper.pw_bridge_bind_client(registry, client_id) -> pw_proxy*

pw._lib_wrapper.pw_bridge_client_update_properties(
    proxy,
    props,              # POINTER(spa_dict)
) -> int
```

`pw_bridge_client_update_properties` merges the given properties into
the client's properties. Properties not present are left unchanged.
To remove a property, pass a NULL value for its key.

### 6.9 Registry destroy

```python
pw._lib_wrapper.pw_bridge_registry_destroy(registry, id) -> int
pw._lib_wrapper.pw_bridge_destroy_link(registry, link_id) -> int
```

Both wrap `pw_registry_destroy`. They work for **any** object type
(node, port, link, client, device). `pw_bridge_destroy_link` is kept
as a compatibility alias.

### 6.10 Core sync

```python
pw._lib_wrapper.pw_bridge_core_sync(core, id, seq) -> int
```

Wraps `pw_core_sync`. Used internally by `_destroy_connection` to
flush pending destroy messages before disconnecting the core.

### 6.11 Version introspection

```python
pw._lib_wrapper.pw_bridge_version_registry() -> int
pw._lib_wrapper.pw_bridge_version_core()     -> int
pw._lib_wrapper.pw_bridge_version_link()     -> int
pw._lib_wrapper.pw_bridge_version_node()     -> int
pw._lib_wrapper.pw_bridge_version_port()     -> int
pw._lib_wrapper.pw_bridge_version_client()   -> int
pw._lib_wrapper.pw_bridge_version_device()   -> int
```

### 6.12 Device

```python
pw._lib_wrapper.pw_bridge_bind_device(registry, device_id) -> pw_proxy*

pw._lib_wrapper.pw_bridge_device_listener_new(
    proxy,
    info_cb,            # PW_BRIDGE_DEVICE_INFO_CB
    user_data,
) -> int

pw._lib_wrapper.pw_bridge_device_listener_free(handle) -> None
```

### 6.13 Node params and SPA pods

```python
pw._lib_wrapper.pw_bridge_node_enum_params(
    proxy, seq, id, start, num, filter) -> int

pw._lib_wrapper.pw_bridge_node_set_param(
    proxy, id, flags, param) -> int

pw._lib_wrapper.pw_bridge_node_param_ids(
    params, n_params, out_ids, max_ids) -> int

pw._lib_wrapper.pw_bridge_pod_parse_props(
    pod, out_volume, out_mute, out_channels,
    max_channels, out_n_channels) -> int

pw._lib_wrapper.pw_bridge_pod_build_props(
    buffer, buffer_size, bits, volume, mute,
    channels, n_channels) -> int
```

---

## 7. Callback signatures

These are the exact Python signatures expected by
`PipeWireRegistry`. All callbacks are invoked from the PipeWire
thread loop, **with the GIL already held by the C wrapper**.

### 7.1 Global added

```python
def on_global_added(
    object_id: int,
    type_str: str,        # decoded interface type
    props: dict,          # decoded spa_dict
) -> None:
    ...
```

Called for every object present in the registry when the registry is
created, and for every new object afterwards. For nodes, ports and
links, this fires **before** the corresponding `_info` callback.

### 7.2 Global removed

```python
def on_global_removed(object_id: int) -> None:
    ...
```

Called when an object disappears. **Not called for the core itself**
(`id=0`); that case is signalled through `on_core_lost` instead.

### 7.3 Node info

```python
def on_node_info(
    node_id: int,
    props: dict,
) -> None:
    ...
```

`props` contains the full node properties, plus the following
internal keys (prefixed with an underscore to distinguish them from
PipeWire properties):

| Key                      | Type | Meaning |
|--------------------------|------|---------|
| `_max_input_ports`       | int  | `info->max_input_ports` |
| `_max_output_ports`      | int  | `info->max_output_ports` |
| `_change_mask`           | int  | `info->change_mask` (bitmask) |
| `_state`                 | int  | `pw_node_state` value |
| `_error`                 | str  | error message if `_state == PW_NODE_STATE_ERROR`, else `""` |
| `_params` | list[int] | list of param ids the node exposes |

The `_change_mask` bits indicate which fields changed. Use them to
avoid re-reading unchanged values.

### 7.4 Port info

```python
def on_port_info(
    port_id: int,
    props: dict,
) -> None:
    ...
```

Internal keys added:

| Key              | Type | Meaning |
|------------------|------|---------|
| `_direction`     | int  | `PW_DIRECTION_INPUT` or `PW_DIRECTION_OUTPUT` |
| `_change_mask`   | int  | `info->change_mask` |

### 7.5 Link info

```python
def on_link_info(
    link_id: int,
    props: dict,
) -> None:
    ...
```

Internal keys added:

| Key                  | Type | Meaning |
|----------------------|------|---------|
| `_output_node_id`    | int  | `info->output_node_id` |
| `_output_port_id`    | int  | `info->output_port_id` |
| `_input_node_id`     | int  | `info->input_node_id` |
| `_input_port_id`     | int  | `info->input_port_id` |
| `_state`             | int  | `pw_link_state` value |
| `_error`             | str  | error message if any |

### 7.6 Core lost

```python
def on_core_lost() -> None:
    ...
```

Called when the connection to PipeWire is lost, either because:

- `global_remove` fired with `id=PW_ID_CORE`, or
- `core error` fired with `id=PW_ID_CORE` and `res == -32` (`EPIPE`).

Called exactly once per loss, even if both conditions trigger. The
reconnect thread will then attempt to restore the connection.

### 7.7 Core restored

```python
def on_core_restored() -> None:
    ...
```

Called after the reconnect thread succeeds in re-establishing the
connection and re-creating the registry listener. At this point the
registry will re-emit `global_added` for every object present in the
server.

---

## 8. `PipeWireRegistry`

### 8.1 Constructor

```python
from pw_bridge import PipeWireRegistry

registry = PipeWireRegistry(
    on_global_added=callable,      # required
    on_global_removed=callable,    # required
    on_node_info=callable,         # optional
    on_port_info=callable,         # optional
    on_link_info=callable,         # optional
    on_core_lost=callable,         # optional
    on_core_restored=callable,     # optional
    thread_name="pw_bridge",       # optional, for debug
)
```

All callbacks are stored as strong references. The registry keeps the
`ctypes.CFUNCTYPE` objects alive for the whole lifetime of the
connection. You do not need to do anything special on your side.

### 8.2 Lifecycle

```python
registry.start()    # calls pw_init, creates connection, starts loop
...
registry.stop()     # idempotent, safe to call twice
```

`start()` raises `RuntimeError` if the initial connection fails. Once
started, the registry spawns a **reconnect thread** (a Python
`threading.Thread`) that monitors `_core_lost` and re-creates the
connection when needed.

`stop()`:

- signals the reconnect thread and joins it (5 s timeout),
- tears down all proxies and listeners under the thread loop lock,
- issues a `pw_core_sync` so the server processes pending messages,
- stops the thread loop,
- disconnects the core, destroys the context, destroys the thread
  loop.

After `stop()`, the registry cannot be restarted. Create a new
instance if you need to reconnect manually.

### 8.3 Public attributes

```python
registry.objects: dict[int, tuple[str, dict]]
```

The authoritative in-memory snapshot of the PipeWire graph.

- Key: object `global_id`.
- Value: `(type_str, props_dict)` where `type_str` is one of the
  `PW_TYPE_INTERFACE_*` constants and `props_dict` is the merged
  result of `global_added` props and the corresponding `_info`
  callback.

Access is protected by an internal `threading.RLock`. Reading the
dict from the main thread is safe; the registry does not return a
copy, so do not mutate it from outside.

### 8.4 Public methods

```python
registry.find_object(type_str: str) -> dict[int, dict]
```

Returns a new dict of `{id: props}` for all objects whose type
matches `type_str`. Example:

```python
nodes = registry.find_object(pw.PW_TYPE_INTERFACE_Node)
for node_id, props in nodes.items():
    print(node_id, props.get("node.name"))
```

```python
registry.get_object(object_id: int) -> Optional[tuple[str, dict]]
```

Returns `(type_str, props)` for one object, or `None`.

```python
registry.create_link(out_port_id: int, in_port_id: int) -> bool
```

Creates a link between two ports. Must be called from a thread other
than the PipeWire loop thread. Returns `True` on success. The real
`link_id` is not known immediately; the proxy is registered as
"pending" and reconciled when the first `link.info` fires.

```python
registry.destroy_link(link_id: int) -> bool
```

Destroys any object by ID (the name is historical). Returns `True` on
success.

```python
registry.rename_client(client_id: int, new_name: str) -> bool
```

Sets `application.name` on a client through
`pw_client_update_properties`. Returns `True` on success.

### 8.5 Node props

```python
registry.get_node_props(node_id, timeout=2.0) -> dict
```

Returns the parsed SPA_PARAM_Props for a node, with keys `volume` (float), `mute` (bool) and `channelVolumes` (list of float), for whichever fields are present.

Behaviour:

- Fast path: returns the cached dict if available.
- If the node does not expose SPA_PARAM_Props, returns `{}` immediately, without a server round-trip.
- If the node is unknown (not bound), raises `KeyError`.
- Must not be called from a PipeWire callback: raises `RuntimeError` if it detects it is running on the loop thread.

```python
registry.request_node_props(node_id) -> bool
```

Asynchronous variant. Issues enum_params and returns immediately. The result is delivered through `on_node_params` and cached. Safe to call from any context, including PipeWire callbacks.

```python
registry.get_node_params_available(node_id) -> set[int] or None
```

Returns the set of param ids the node exposes, as reported by its last `node.info`. Returns `None` if `node.info` has not yet been received.

```python
registry.set_node_volume(node_id, volume: float) -> bool
registry.set_node_mute(node_id, mute: bool) -> bool
registry.set_node_channel_volumes(node_id, volumes: list[float]) -> bool
```

Build a SPA_PARAM_Props pod with the given fields and send it with set_param. Any other field of the node's Props is left untouched. After a successful set, the node's cache entry is invalidated so the next `get_node_props` reads the value back from the server.

---

## 9. Ownership and lifetime rules

This section is critical. Violating these rules will produce
segfaults, memory leaks or PipeWire errors like
`impl_ext_end_proxy called from wrong context`.

### 9.1 Listeners and proxies are owned by the C wrapper

When you call `pw_bridge_*_listener_new`, the C wrapper allocates a
`struct pw_bridge_*_listener` on the heap and returns an opaque
handle. You must **not** try to read or write its fields from
Python. The only valid operations are:

- store the handle,
- pass it to `pw_bridge_*_listener_free` exactly once.

Same for proxies returned by `pw_bridge_bind_*` and
`pw_bridge_create_link`: they are `pw_proxy*` owned by the client.
Call `pw.proxy_destroy(proxy)` exactly once.

### 9.2 Destruction order matters

Correct teardown sequence (what `_destroy_connection` does):

1. Free every listener (`*_listener_free`) while the corresponding
   proxy is still alive.
2. Destroy every proxy (`pw.proxy_destroy`).
3. Issue `pw_bridge_core_sync` to flush pending destroy messages.
4. Free the registry and core listeners.
5. Stop the thread loop.
6. Disconnect the core.
7. Destroy the context.
8. Destroy the thread loop.

Steps 1–4 must be performed **under the thread loop lock**.
`pw_thread_loop_stop` must be performed **outside** the lock,
otherwise it deadlocks.

### 9.3 Never call PipeWire functions from an arbitrary thread

Any call that touches a proxy, a hook or the core is context-sensitive.
In practice:

- **From a PipeWire callback**: you are already in the loop thread,
  no lock needed.
- **From any other thread**: take
  `pw_bridge_thread_loop_lock(loop)` before the call and
  `pw_bridge_thread_loop_unlock(loop)` after.

`pw_thread_loop_lock` is recursive, so taking it from a callback is
harmless. The current implementation of `_destroy_connection` uses
this property to have a single code path for all callers.

### 9.4 No Python callbacks during teardown

Once `stop()` has been called, the registry sets `_running = False`
and joins the reconnect thread. Callbacks may still fire during the
join if events were already queued in the loop. Your callbacks should
be defensive against being called after `stop()` (for example, check
a flag before doing UI updates).

### 9.5 The GIL is held by the C wrapper

Your Python callbacks are invoked with the GIL already held by
`PyGILState_Ensure`. Do not attempt to acquire it again (no
`with some_lock` around GIL-sensitive operations). Use your own
`threading.Lock` or `RLock` to protect shared Python state between
the callback thread and the main thread.

---

## 10. Reconnection model

The registry runs a background thread that:

1. Waits on `self._reconnect_stop.wait(1.0)` (interruptible).
2. When woken and `self._core_lost` is `True`:
   - calls `_destroy_connection` (clean teardown of the dead
     connection),
   - calls `_create_connection` (fresh thread loop, context, core,
     registry, listeners),
   - calls `on_core_restored` if successful.

The thread is stopped by setting `self._reconnect_stop` and joining
with a 5 s timeout in `stop()`.

`on_core_lost` is called exactly once per loss. `on_core_restored` is
called exactly once per successful reconnection.

---

## 11. Common pitfalls

### 11.1 Taking the thread loop lock while the loop is stopped

Do **not** call `pw_bridge_thread_loop_lock` after
`pw_thread_loop_stop`. The correct order in `_destroy_connection` is:
lock, unbind, sync, free listeners, unlock, **then** stop.

### 11.2 Calling `pw_registry_destroy` from a PipeWire callback

This is legal, but the callback is already in the loop thread, so the
destroy message is queued immediately. Do not call
`pw_bridge_thread_loop_lock` there — it is a no-op because the lock
is recursive, but it is unnecessary.

### 11.3 Relying on `object_id` for pending proxies

When you call `create_link`, the returned proxy does not yet have a
global ID. `pw_proxy_get_id` returns a client-local ID. The global
`link_id` is only known when `link.info` fires. The registry handles
this internally by keeping a `_pending_link_proxies` list and
reconciling on the first `link.info`.

### 11.4 Accessing `registry.objects` without the lock

`registry.objects` is protected by `_objects_lock`. If you iterate
over it from the main thread while a callback is inserting an entry,
you may raise `RuntimeError: dictionary changed size during
iteration`. Always go through `find_object` / `get_object`, or copy
the dict under the lock before iterating.

### 11.5 Mixing `pw_init` and `pw_deinit`

`pw_init` is global. In a process that creates and destroys many
`PipeWireRegistry` instances, call `pw_init` once at startup and
`pw_deinit` once at exit. Do not call them from `start()` /
`stop()`, even though `start()` currently does call `pw_init`.

A future release will move `pw_init` to a module-level
`pw_bridge.init()` function. For now, calling `pw_init` twice is safe
because PipeWire reference-counts it.

### 11.6 The server does not echo your seq

When you call `pw_node_enum_params(proxy, seq, ...)`, the `seq` you provide is not what comes back in the `param` event. The server assigns its own sequence numbers, incremented by 2 per round-trip (one for the enum_params, one for a paired core_sync). You cannot correlate responses with requests by seq.

`PipeWireRegistry` implements a FIFO of pending requests: it sends a `core_sync` right after `enum_params` and releases the waiting `get_node_props` when the sync completes, whether or not a param event was seen in the meantime.

---

## 12. Examples

### 12.1 Minimal reader

```python
import logging
from pw_bridge import PipeWireRegistry
from pw_bridge import pw_bindings as pw

logging.basicConfig(level=logging.INFO)

def on_added(oid, type_str, props):
    if type_str == pw.PW_TYPE_INTERFACE_Node:
        print(f"node {oid}: {props.get('node.name')}")

def on_removed(oid):
    print(f"gone: {oid}")

def on_node_info(node_id, props):
    state = props.get("_state")
    print(f"  {node_id} state={pw.state_name(state)}")

registry = PipeWireRegistry(
    on_global_added=on_added,
    on_global_removed=on_removed,
    on_node_info=on_node_info,
)

registry.start()

try:
    import time
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    registry.stop()
```

### 12.2 Create a link programmatically

```python
# Find an output port and an input port from the in-memory snapshot.
out_port = None
in_port = None
for oid, props in registry.find_object(pw.PW_TYPE_INTERFACE_Port).items():
    if props.get("_direction") == pw.PW_DIRECTION_OUTPUT:
        out_port = oid
    elif props.get("_direction") == pw.PW_DIRECTION_INPUT:
        in_port = oid
    if out_port and in_port:
        break

if out_port and in_port:
    ok = registry.create_link(out_port, in_port)
    print("created:", ok)
```

### 12.3 React to core loss and restoration

```python
def on_lost():
    print("PipeWire went away")

def on_restored():
    print("PipeWire is back")

registry = PipeWireRegistry(
    on_global_added=on_added,
    on_global_removed=on_removed,
    on_core_lost=on_lost,
    on_core_restored=on_restored,
)
registry.start()
```

---

## 13. What is not (yet) exposed

The following PipeWire features are not currently available
through pw-bridge. They are candidates for future additions, in
priority order for PCC.

### 13.1 Device parameters (profiles, routes)

- `pw_device_enum_params` / `pw_device_set_param`
- `SPA_PARAM_Profile`, `SPA_PARAM_EnumProfile`,
  `SPA_PARAM_Route`, `SPA_PARAM_EnumRoute`

Would allow reading and writing device profiles and routes.
Requires extending the pod helpers in the C wrapper to handle
the nested structs used by profiles and routes (the current
helpers only cover scalar fields and arrays, which is enough
for Props but not for Profile).

### 13.2 Profiler (xruns, DSP load, quantum)

- `pw_profiler` listener
- `PipeWire:Interface:Profiler`

Would replace `pw-top` for PCC's status tab. Requires loading
the `libpipewire-module-profiler` in PipeWire (either via the
server configuration, or by calling `pw_context_load_module`).

### 13.3 Port format negotiation

- `SPA_PARAM_Format` on ports

Would allow displaying the negotiated format of a link
(sample rate, channel count, sample format). Requires the same
pod parsing extension as §13.1.

### 13.4 Client permissions

- `pw_client_update_permissions`
- `pw_client_get_permissions`

Rarely needed. Only relevant for tools that grant or revoke
access at runtime.

## 14. Rule for future additions

Before adding anything to the C wrapper, ask:

1. **Is the corresponding PipeWire function static inline?**
   Run `nm -D libpipewire-0.3.so.0 | grep <name>`.
   - If exported: add a `_lib.<name>.argtypes` declaration in
     `pw_bindings.py`. Do not touch the C wrapper.
   - If not exported: it goes into `pw_bridge.c`.

2. **Does it need per-instance `user_data`?**
   If yes, it must be a `_listener_new` / `_listener_free` pair, with
   the same pattern as node / port / link.

3. **Does it need the GIL?**
   If the callback is invoked from the PipeWire loop thread, wrap the
   Python call in `PyGILState_Ensure` / `PyGILState_Release`.

4. **Does it touch `spa_pod`?**
   If yes, it probably belongs in the C wrapper, because reading a
   `spa_pod` from Python requires reimplementing the SPA parser.

---

## 15. Testing

```bash
cd pw-bridge
python3 tests/test_two_registries.py -v
python3 tests/test_metadata.py -v
python3 tests/test_device.py -v
python3 tests/test_node_props.py -v
```

The test suite requires a running PipeWire session. Each file
validates a specific area:

| File                          | What it validates |
|-------------------------------|-------------------|
| `test_two_registries.py`      | Instance isolation (two registries, no cross-delivery), listener lifecycle, link create/destroy roundtrip, idempotent `stop()`, bounded reconnect thread join, interleaved start/stop. |
| `test_metadata.py`            | Metadata is bound and populated, `get_metadata_json` decodes JSON values, `set_metadata_json` roundtrip and restore, `stop()` releases metadata listeners and proxies. Skipped if the server does not expose a Metadata object. |
| `test_device.py`              | At least one Device is bound and visible, `on_device_info` fires with a non-empty `device.name`, props are merged into `registry.objects`, `stop()` releases listeners and proxies. Skipped if no Device is present. |
| `test_node_props.py`          | `get_node_props` returns volume/mute/channels, `set_node_volume` and `set_node_mute` roundtrip via the server, deadlock guard when called from the loop thread, `KeyError` on unknown nodes. Skipped if no node exposes SPA_PARAM_Props. |

Expected output for each file:

```bash
Ran N tests in X.XXXs

OK
```

Tests may report `skipped` when the running session does not
provide the objects they need (for example, no metadata module
loaded, no device, or no node with Props). This is expected.

If the tests print `impl_ext_end_proxy called from wrong context`
or `unknown resource N op:7` messages, it means the thread loop
lock is not held during a destroy path. Check
`_destroy_connection`.

If `test_node_props.py` prints `enum params id:2 failed` at
debug level, it means the server was asked for Props on a node
that does not expose them. This should not happen: the
`_node_params_available` set is consulted before issuing the
request. See §11.6 for the seq correlation issue.
```

End of document.
