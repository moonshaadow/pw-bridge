#!/usr/bin/env python3
"""Functional tests for node SPA_PARAM_Props (volume, mute, channels).

Usage:
    python3 tests/test_node_props.py -v
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


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestNodeProps(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _pipewire_available():
            raise unittest.SkipTest("PipeWire session not available.")

    def setUp(self):
        self._registries = []
        self._events = []
        self._events_lock = threading.RLock()

    def tearDown(self):
        for reg in self._registries:
            try:
                reg.stop()
            except Exception:
                logging.exception("Error stopping registry")

    def _on_node_params(self, node_id, seq, props):
        with self._events_lock:
            self._events.append((node_id, seq, dict(props)))

    def _start(self):
        reg = PipeWireRegistry(
            on_global_added=lambda oid, t, props: None,
            on_global_removed=lambda oid: None,
            on_node_params=self._on_node_params,
            thread_name="pw_bridge_props_test",
        )
        self._registries.append(reg)
        reg.start()
        return reg

    def _find_node_with_props(self, reg, timeout=5.0):
        """Return the id of the first node that exposes Props."""
        deadline = time.monotonic() + timeout
        tried = set()
        while time.monotonic() < deadline:
            nodes = reg.find_object(pw.PW_TYPE_INTERFACE_Node)
            for nid in nodes:
                if nid in tried:
                    continue
                avail = reg.get_node_params_available(nid)
                if avail is None:
                    continue
                tried.add(nid)
                if pw.SPA_PARAM_Props not in avail:
                    continue
                try:
                    parsed = reg.get_node_props(nid, timeout=1.0)
                except Exception:
                    continue
                if any(k in parsed for k in
                       ("volume", "mute", "channelVolumes")):
                    return nid
            time.sleep(0.1)
        return None

    # ------------------------------------------------------------------

    def test_get_node_props(self):
        reg = self._start()
        self.assertTrue(wait_for(
            lambda: len(reg.find_object(pw.PW_TYPE_INTERFACE_Node)) > 0))
        nid = self._find_node_with_props(reg)
        if nid is None:
            self.skipTest("No node exposes SPA_PARAM_Props")
        parsed = reg.get_node_props(nid)
        self.assertTrue(
            any(k in parsed for k in
                ("volume", "mute", "channelVolumes")),
            f"Expected volume/mute/channels, got {parsed}")

    def test_set_node_volume(self):
        reg = self._start()
        self.assertTrue(wait_for(
            lambda: len(reg.find_object(pw.PW_TYPE_INTERFACE_Node)) > 0))
        nid = self._find_node_with_props(reg)
        if nid is None:
            self.skipTest("No node exposes SPA_PARAM_Props")
        original = reg.get_node_props(nid)
        if "volume" not in original:
            self.skipTest("Node does not expose scalar volume")
        v0 = original["volume"]
        new_v = min(v0 * 0.5, 1.0)
        try:
            ok = reg.set_node_volume(nid, new_v)
            self.assertTrue(ok)
            self.assertTrue(wait_for(
                lambda: abs(reg.get_node_props(nid).get("volume", 0)
                            - new_v) < 1e-3,
                timeout=3.0))
        finally:
            reg.set_node_volume(nid, v0)

    def test_set_node_mute(self):
        reg = self._start()
        self.assertTrue(wait_for(
            lambda: len(reg.find_object(pw.PW_TYPE_INTERFACE_Node)) > 0))
        nid = self._find_node_with_props(reg)
        if nid is None:
            self.skipTest("No node exposes SPA_PARAM_Props")
        original = reg.get_node_props(nid)
        if "mute" not in original:
            self.skipTest("Node does not expose mute")
        m0 = original["mute"]
        try:
            ok = reg.set_node_mute(nid, not m0)
            self.assertTrue(ok)
            self.assertTrue(wait_for(
                lambda: reg.get_node_props(nid).get("mute") == (not m0),
                timeout=3.0))
        finally:
            reg.set_node_mute(nid, m0)

    def test_get_node_props_from_callback_raises(self):
        """Verify the guard against calling get_node_props from the
        PipeWire loop thread. We simulate the loop thread identity
        to test the guard in isolation."""
        reg = self._start()
        reg._loop_thread_id = threading.get_ident()
        try:
            with self.assertRaises(RuntimeError):
                reg.get_node_props(1)
        finally:
            reg._loop_thread_id = None

    def test_get_node_props_unknown_node(self):
        reg = self._start()
        with self.assertRaises(KeyError):
            reg.get_node_props(0x7FFFFFF0, timeout=0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestNodeProps)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())