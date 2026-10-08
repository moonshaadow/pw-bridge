#!/usr/bin/env python3
"""Functional tests for the PipeWire device interface.

These tests validate that:

  D1. At least one Device object is bound after start(), and its
      properties are populated.

  D2. The device.info callback provides the expected textual
      properties (device.name, device.api, ...).

  D3. stop() releases the device listeners and proxies cleanly.

The tests require a running PipeWire session with at least one
device (typically an ALSA card or a virtual device). If no device is
present, the tests are skipped.

Usage:
    python3 tests/test_device.py
    python3 tests/test_device.py -v
"""

import argparse
import logging
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pw_bridge import PipeWireRegistry  # noqa: E402
from pw_bridge import pw_bindings as pw  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _pipewire_available() -> bool:
    try:
        pw._lib.pw_init(None, None)
    except Exception:
        return False

    loop = pw._lib.pw_thread_loop_new(b"pw_bridge_probe", None)
    if not loop:
        return False
    try:
        ctx = pw._lib.pw_context_new(
            pw._lib.pw_thread_loop_get_loop(loop), None, 0)
        if not ctx:
            return False
        try:
            core = pw._lib.pw_context_connect(ctx, None, 0)
            if not core:
                return False
            pw._lib.pw_core_disconnect(core)
        finally:
            pw._lib.pw_context_destroy(ctx)
    finally:
        pw._lib.pw_thread_loop_destroy(loop)
    return True


def _server_has_devices() -> bool:
    """Return True if the server exposes at least one Device object."""
    found = []

    def on_added(oid, type_str, props):
        if type_str == pw.PW_TYPE_INTERFACE_Device:
            found.append(oid)

    reg = PipeWireRegistry(
        on_global_added=on_added,
        on_global_removed=lambda oid: None,
        thread_name="pw_bridge_dev_probe",
    )
    try:
        reg.start()
        wait_for(lambda: bool(found), timeout=2.0)
    finally:
        reg.stop()
    return bool(found)


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class DeviceRecorder:
    """Thread-safe recorder for device info callbacks."""

    def __init__(self):
        self.lock = threading.RLock()
        self.events = []

    def on_info(self, device_id, props):
        with self.lock:
            self.events.append((device_id, dict(props)))

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def clear(self):
        with self.lock:
            self.events.clear()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

class TestDevice(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _pipewire_available():
            raise unittest.SkipTest("PipeWire session not available.")
        if not _server_has_devices():
            raise unittest.SkipTest(
                "PipeWire server does not expose any Device object.")

    def setUp(self):
        self._registries = []
        self._recorder = DeviceRecorder()

    def tearDown(self):
        for reg in self._registries:
            try:
                reg.stop()
            except Exception:
                logging.exception("Error stopping registry in tearDown")
        self._registries.clear()

    def _start(self):
        reg = PipeWireRegistry(
            on_global_added=lambda oid, t, props: None,
            on_global_removed=lambda oid: None,
            on_device_info=self._recorder.on_info,
            thread_name="pw_bridge_device_test",
        )
        self._registries.append(reg)
        reg.start()
        return reg

    # ------------------------------------------------------------------
    # D1: at least one device bound, props populated
    # ------------------------------------------------------------------

    def test_device_populated(self):
        reg = self._start()

        ok = wait_for(
            lambda: len(reg.find_object(pw.PW_TYPE_INTERFACE_Device)) > 0,
            timeout=5.0)
        self.assertTrue(
            ok,
            "At least one Device object should be visible after start()")

        self.assertGreater(
            len(reg._device_proxies), 0,
            "At least one Device proxy should have been bound")
        self.assertGreater(
            len(reg._device_listeners), 0,
            "At least one Device listener should have been attached")

    # ------------------------------------------------------------------
    # D2: device.info fires with textual properties
    # ------------------------------------------------------------------

    def test_device_info_callback(self):
        reg = self._start()

        ok = wait_for(
            lambda: len(self._recorder.snapshot()) > 0,
            timeout=5.0)
        self.assertTrue(
            ok,
            "on_device_info should fire for at least one device")

        events = self._recorder.snapshot()
        # Look for one event with a device.name property.
        named = [
            (did, p) for did, p in events
            if isinstance(p.get("device.name"), str)
            and p.get("device.name")
        ]
        self.assertTrue(
            named,
            "At least one device.info should carry a non-empty "
            "device.name; got events=%r" % (events,))

        dev_id, props = named[0]
        # Sanity: the device is also present in the objects dict with
        # its info merged in.
        with reg._objects_lock:
            entry = reg.objects.get(dev_id)
        self.assertIsNotNone(
            entry,
            "Device %d should be present in registry.objects" % dev_id)
        type_str, merged = entry
        self.assertEqual(type_str, pw.PW_TYPE_INTERFACE_Device)
        self.assertEqual(merged.get("device.name"),
                         props.get("device.name"))
        self.assertIn("_change_mask", merged)

    # ------------------------------------------------------------------
    # D3: stop() releases device listeners and proxies
    # ------------------------------------------------------------------

    def test_stop_releases_devices(self):
        reg = self._start()
        time.sleep(0.5)

        self.assertGreater(len(reg._device_proxies), 0)
        self.assertGreater(len(reg._device_listeners), 0)

        reg.stop()

        self.assertEqual(len(reg._device_proxies), 0)
        self.assertEqual(len(reg._device_listeners), 0)
        self.assertFalse(reg._running)

        self._registries.remove(reg)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestDevice)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())