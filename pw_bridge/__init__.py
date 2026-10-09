"""Shared PipeWire bridge for PCC and Patchanka.

This package provides:
- ctypes bindings to libpipewire-0.3 (via pw_bindings)
- a C wrapper (libpw_bridge.so) for static inline functions
- a high-level registry/event manager (via registry)
"""

__version__ = "0.2.2"

from . import pw_bindings
from .registry import PipeWireRegistry

__all__ = ['pw_bindings', 'PipeWireRegistry', '__version__']

