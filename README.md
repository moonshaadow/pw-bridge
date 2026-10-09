# pw-bridge

Shared PipeWire C bridge and Python bindings for
[Patchanka](https://github.com/moonshaadow/Patchanka) and
[PipeWire Control Center](https://github.com/moonshaadow/pipewire-control-center).

A small C wrapper exposes the PipeWire functions that are
`static inline` in the headers and therefore not exported by
`libpipewire-0.3.so.0`. Everything else is called directly from
Python via ctypes.

## Requirements

- Linux with PipeWire 1.0+
- Python 3.8+
- `libpipewire-0.3-dev`

## Build

```bash
cd native
./build.sh
```

This produces `native/libpw_bridge.so`, which the Python package
loads automatically.

## Usage

```python
from pw_bridge import PipeWireRegistry

registry = PipeWireRegistry(
    on_global_added=lambda oid, t, props: print(oid, t),
    on_global_removed=lambda oid: print("gone", oid),
)
registry.start()
# ...
registry.stop()
```

## Documentation

See [docs/pw-bridge_API_ref_0.2.2.md](docs/pw-bridge_API_ref_0.2.2.md)
for the full API reference.

## License

GPL-2.0-or-later.

## Changelog

- **0.2.2** — Metadata targeting by id, `find_metadata_by_name`,
  loop thread guard fix. Breaking change on the metadata API.
- **0.2.1** — Device interface, node SPA_PARAM_Props, metadata fixes.
- **0.2.0** — Initial public release.
