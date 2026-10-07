"""Shared PipeWire bridge for PCC and Patchanka.

This package provides:
- ctypes bindings to libpipewire-0.3 (via pw_bindings)
- a C wrapper (libpw_bridge.so) for static inline functions
- a high-level registry/event manager (via pipewire_registry)
"""

from . import pw_bindings
from .pipewire_registry import PipeWireRegistry

__all__ = ['pw_bindings', 'PipeWireRegistry']