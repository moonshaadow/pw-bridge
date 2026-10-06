# pw-bridge

Shared PipeWire C bridge and Python bindings for PCC and Patchanka.

## Components

- `native/pw_bridge.c` : C wrapper exposing PipeWire functions that cannot be called directly via ctypes (static inline functions).
- `pw_bridge/pw_bindings.py` : ctypes bindings for libpipewire-0.3 and the C wrapper.
- `pw_bridge/registry.py` : high-level registry and event manager.

## Build

```bash
cd native
make
```
This produces libpw_bridge.so.

## Usage

```bash
from pw_bridge import PipeWireRegistry
from pw_bridge import pw_bindings as pw

registry = PipeWireRegistry(
    on_global_added=...,
    on_global_removed=...,
)
registry.start()
```
## License

GPL v2 or later
