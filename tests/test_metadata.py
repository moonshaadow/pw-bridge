#!/usr/bin/env python3
"""Functional tests for the PipeWire metadata interface.

These tests are skipped automatically when:

  - no PipeWire session is available, or
  - the libpipewire version is older than 1.2 and the metadata
    interface was not compiled into the C wrapper, or
  - the running PipeWire server does not expose any Metadata
    object (module not loaded).

Usage:
    python3 tests/test_metadata.py
    python3 tests/test_metadata.py -v
"""

import argparse
import json
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


def _server_has_metadata() -> bool:
    """Return True if the server exposes at least one Metadata object."""
    found = []

    def on_added(oid, type_str, props):
        if type_str == pw.PW_TYPE_INTERFACE_Metadata:
            found.append(oid)

    reg = PipeWireRegistry(
        on_global_added=on_added,
        on_global_removed=lambda oid: None,
        thread_name="pw_bridge_meta_probe",
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


class MetadataRecorder:
    """Thread-safe recorder for metadata changes."""

    def __init__(self):
        self.lock = threading.RLock()
        self.events = []

    def on_changed(self, metadata_id, subject, key, type_, value):
        with self.lock:
            self.events.append(
                (metadata_id, subject, key, type_, value))

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def clear(self):
        with self.lock:
            self.events.clear()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

class TestMetadata(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _pipewire_available():
            raise unittest.SkipTest(
                "PipeWire session not available.")
        if not pw.HAVE_METADATA:
            raise unittest.SkipTest(
                "Metadata interface not available in this libpipewire "
                "version (requires PipeWire >= 1.2).")
        if not _server_has_metadata():
            raise unittest.SkipTest(
                "PipeWire server does not expose any Metadata object. "
                "Load libpipewire-module-metadata in the server "
                "configuration.")

    def setUp(self):
        self._registries = []
        self._recorder = MetadataRecorder()

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
            on_metadata_changed=self._recorder.on_changed,
            thread_name="pw_bridge_metadata_test",
        )
        self._registries.append(reg)
        reg.start()
        return reg

    # ------------------------------------------------------------------
    # M1: metadata is bound and populated
    # ------------------------------------------------------------------

    def test_metadata_populated(self):
        reg = self._start()

        ok = wait_for(
            lambda: (reg.get_metadata(0, "clock.rate") is not None
                     and reg.get_metadata(0, "clock.quantum") is not None),
            timeout=5.0)
        self.assertTrue(
            ok,
            "clock.rate and clock.quantum should be present after "
            "start(); got %r" % (reg.metadata,))

        self.assertGreater(
            len(reg._metadata_proxies), 0,
            "At least one Metadata object should have been bound")

    # ------------------------------------------------------------------
    # M2: get_metadata_json decodes JSON values
    # ------------------------------------------------------------------

    def test_get_metadata_json(self):
        reg = self._start()

        self.assertTrue(
            wait_for(lambda: reg.get_metadata(0, "clock.rate") is not None,
                     timeout=5.0),
            "clock.rate should be present after start()")

        rate = reg.get_metadata_json(0, "clock.rate")
        self.assertIsInstance(
            rate, int,
            "clock.rate should decode to an int, got %r" % (rate,))
        self.assertGreater(rate, 0)

        raw_entry = reg.get_metadata(0, "clock.rate")
        self.assertIsNotNone(raw_entry)
        type_s, raw_s = raw_entry
        self.assertIn(
            type_s, ("", pw.SPA_TYPE_STRING_JSON),
            "clock.rate should be either untyped or Spa:String:JSON, "
            "got %r" % (type_s,))
        self.assertEqual(json.loads(raw_s), rate)
        
    # ------------------------------------------------------------------
    # M3 + M4: set_metadata_json roundtrip and restore
    # ------------------------------------------------------------------

    def test_set_metadata_json_roundtrip(self):
        reg = self._start()

        self.assertTrue(
            wait_for(lambda: reg.get_metadata(0, "clock.rate") is not None,
                     timeout=5.0),
            "clock.rate should be present after start()")

        original = reg.get_metadata_json(0, "clock.rate")
        self.assertIsInstance(original, int)
        self.assertGreater(original, 0)

        self._recorder.clear()

        try:
            new_rate = original * 2
            ok = reg.set_metadata_json(0, "clock.rate", new_rate)
            self.assertTrue(ok, "set_metadata_json should return True")

            def observed():
                for _mid, subj, key, _t, val in self._recorder.snapshot():
                    if subj == 0 and key == "clock.rate":
                        try:
                            v = json.loads(val) if val else None
                        except ValueError:
                            v = None
                        if v == new_rate:
                            return True
                return False

            self.assertTrue(
                wait_for(observed, timeout=5.0),
                "on_metadata_changed should report clock.rate=%d; "
                "events=%r" % (new_rate, self._recorder.snapshot()))

            self.assertEqual(
                reg.get_metadata_json(0, "clock.rate"),
                new_rate)

        finally:
            self._recorder.clear()
            ok = reg.set_metadata_json(0, "clock.rate", original)
            self.assertTrue(ok, "restore should return True")

            def restored():
                return reg.get_metadata_json(0, "clock.rate") == original

            self.assertTrue(
                wait_for(restored, timeout=5.0),
                "clock.rate should be restored to %d" % original)

    # ------------------------------------------------------------------
    # M5: stop() releases the metadata listeners and proxies
    # ------------------------------------------------------------------

    def test_stop_releases_metadata(self):
        reg = self._start()
        time.sleep(0.5)

        self.assertGreater(len(reg._metadata_proxies), 0)
        self.assertGreater(len(reg._metadata_listeners), 0)

        reg.stop()

        self.assertEqual(len(reg._metadata_proxies), 0)
        self.assertEqual(len(reg._metadata_listeners), 0)
        self.assertFalse(reg._running)
        with reg._objects_lock:
            self.assertEqual(len(reg.metadata), 0)

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
    suite = loader.loadTestsFromTestCase(TestMetadata)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())