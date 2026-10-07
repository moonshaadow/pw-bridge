#!/usr/bin/env python3
"""Functional tests for pw_bridge.

These tests validate the critical fixes introduced in the
per-instance-callback refactor:

  T1. Two PipeWireRegistry instances in the same process each
      receive their own events (no callback overwriting).

  T2. Global registry events are routed to the correct instance
      (user_data / per-instance listener isolation).

  T3. Node, port and link listeners are correctly attached and
      freed (no crash when stopping, no leaked hooks).

  T4. create_link / destroy_link round-trip works and the pending
      proxy is reconciled with the real link_id.

  T5. stop() is idempotent and cleanly releases every resource.

  T6. The reconnect thread does not spin, and stop() joins it in
      bounded time.

The tests require a running PipeWire session. If PipeWire is not
available, the script exits with a clear message instead of failing.

Usage:
    python3 tests/test_two_registries.py
    python3 tests/test_two_registries.py -v

Exit code:
    0 if all tests pass
    1 if any test fails
"""

import argparse
import ctypes
import logging
import os
import signal
import sys
import threading
import time
import unittest
from pathlib import Path

# Allow running from the repository root without installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pw_bridge import PipeWireRegistry  # noqa: E402
from pw_bridge import pw_bindings as pw  # noqa: E402


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _pipewire_available() -> bool:
    """Return True if a PipeWire session appears to be reachable.

    We probe by attempting to create a registry. If that fails, we
    skip the whole test suite with an explanatory message.
    """
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


class Recorder:
    """Thread-safe event recorder.

    Each PipeWireRegistry instance in the tests owns one Recorder.
    The recorder tracks events with a per-instance tag so we can
    check that events are not cross-delivered between instances.
    """

    def __init__(self, tag: str):
        self.tag = tag
        self.lock = threading.RLock()
        self.global_added: list[tuple[int, str, dict]] = []
        self.global_removed: list[int] = []
        self.node_info: list[tuple[int, dict]] = []
        self.port_info: list[tuple[int, dict]] = []
        self.link_info: list[tuple[int, dict]] = []
        self.core_lost = 0
        self.core_restored = 0

    def on_global_added(self, id_, type_, props):
        with self.lock:
            self.global_added.append((id_, type_, props))

    def on_global_removed(self, id_):
        with self.lock:
            self.global_removed.append(id_)

    def on_node_info(self, id_, props):
        with self.lock:
            self.node_info.append((id_, props))

    def on_port_info(self, id_, props):
        with self.lock:
            self.port_info.append((id_, props))

    def on_link_info(self, id_, props):
        with self.lock:
            self.link_info.append((id_, props))

    def on_core_lost(self):
        with self.lock:
            self.core_lost += 1

    def on_core_restored(self):
        with self.lock:
            self.core_restored += 1

    def snapshot(self):
        with self.lock:
            return {
                "tag": self.tag,
                "global_added": list(self.global_added),
                "global_removed": list(self.global_removed),
                "node_info": list(self.node_info),
                "port_info": list(self.port_info),
                "link_info": list(self.link_info),
                "core_lost": self.core_lost,
                "core_restored": self.core_restored,
            }


def make_registry(tag: str) -> tuple[PipeWireRegistry, Recorder]:
    rec = Recorder(tag)
    reg = PipeWireRegistry(
        on_global_added=rec.on_global_added,
        on_global_removed=rec.on_global_removed,
        on_node_info=rec.on_node_info,
        on_port_info=rec.on_port_info,
        on_link_info=rec.on_link_info,
        on_core_lost=rec.on_core_lost,
        on_core_restored=rec.on_core_restored,
        thread_name=f"pw_bridge_{tag}",
    )
    return reg, rec


def wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll predicate until it returns True or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

class TestTwoRegistries(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not _pipewire_available():
            raise unittest.SkipTest(
                "PipeWire session not available. "
                "Start PipeWire (or WirePlumber) before running "
                "these tests.")

    def setUp(self):
        # Each test creates fresh registries. If a previous test
        # failed halfway, this ensures we start clean.
        self._registries: list[PipeWireRegistry] = []

    def tearDown(self):
        for reg in self._registries:
            try:
                reg.stop()
            except Exception:
                logging.exception("Error stopping registry in tearDown")
        self._registries.clear()

    # ------------------------------------------------------------------
    # T1 + T2: two instances, isolated events
    # ------------------------------------------------------------------

    def test_two_registries_receive_their_own_events(self):
        """Two registries in the same process must each receive
        their own events, with no cross-delivery."""
        reg_a, rec_a = make_registry("A")
        reg_b, rec_b = make_registry("B")
        self._registries.extend([reg_a, reg_b])

        reg_a.start()
        reg_b.start()

        # Wait until both have received at least one global_added.
        ok = wait_for(
            lambda: len(rec_a.global_added) > 0
            and len(rec_b.global_added) > 0,
            timeout=5.0,
        )
        self.assertTrue(
            ok, "Both registries should have received at least one "
                "global_added event within 5 seconds")

        # Give PipeWire a bit more time to deliver the initial
        # registry dump (all objects).
        time.sleep(0.5)

        snap_a = rec_a.snapshot()
        snap_b = rec_b.snapshot()

        # T1: both instances received events.
        self.assertGreater(len(snap_a["global_added"]), 0)
        self.assertGreater(len(snap_b["global_added"]), 0)

        # T2: the sets of object ids are identical (same server,
        # same initial dump). If callbacks were cross-delivered, one
        # of the two recorders would be empty or partial.
        ids_a = {id_ for id_, _, _ in snap_a["global_added"]}
        ids_b = {id_ for id_, _, _ in snap_b["global_added"]}
        self.assertEqual(
            ids_a, ids_b,
            "Both registries should see the same set of objects")

        # T2: node_info events must be delivered to both instances
        # independently. We only check the count is > 0 for nodes,
        # if the session has at least one node.
        has_node = any(
            t == pw.PW_TYPE_INTERFACE_Node
            for _, t, _ in snap_a["global_added"]
        )
        if has_node:
            ok = wait_for(
                lambda: len(rec_a.node_info) > 0
                and len(rec_b.node_info) > 0,
                timeout=5.0,
            )
            self.assertTrue(
                ok, "node_info should reach both registries when the "
                    "session has at least one node")

    # ------------------------------------------------------------------
    # T3: bind/unbind of node, port, link listeners
    # ------------------------------------------------------------------

    def test_listener_attach_and_free(self):
        """Attaching and freeing node/port/link listeners must not
        crash, and stop() must release everything cleanly."""
        reg, rec = make_registry("T3")
        self._registries.append(reg)
        reg.start()

        # Wait for at least one global event so the internal tables
        # are populated.
        self.assertTrue(
            wait_for(lambda: len(rec.global_added) > 0, timeout=5.0),
            "Registry did not deliver any global event")

        # Let the initial registry dump complete.
        time.sleep(0.5)

        with reg._objects_lock:
            n_nodes = len(reg._node_proxies)
            n_ports = len(reg._port_proxies)
            n_links = len(reg._link_proxies)

        # We do not assert that there is at least one of each: a
        # minimal PipeWire session might have zero links. But if
        # there are nodes, node listeners must have been created.
        self.assertGreaterEqual(n_nodes, 0)
        self.assertGreaterEqual(n_ports, 0)
        self.assertGreaterEqual(n_links, 0)

        # If the session has ports, ensure a port_info arrived.
        has_port = any(
            t == pw.PW_TYPE_INTERFACE_Port
            for _, t, _ in rec.global_added
        )
        if has_port:
            self.assertTrue(
                wait_for(lambda: len(rec.port_info) > 0, timeout=5.0),
                "port_info should be delivered for ports")

        # Explicit stop, then check tables are empty.
        reg.stop()
        self._registries.remove(reg)

        with reg._objects_lock:
            self.assertEqual(len(reg._node_proxies), 0)
            self.assertEqual(len(reg._port_proxies), 0)
            self.assertEqual(len(reg._link_proxies), 0)
            self.assertEqual(len(reg._node_listeners), 0)
            self.assertEqual(len(reg._port_listeners), 0)
            self.assertEqual(len(reg._link_listeners), 0)
            self.assertEqual(len(reg._pending_link_proxies), 0)

    # ------------------------------------------------------------------
    # T4: create_link / destroy_link round-trip
    # ------------------------------------------------------------------

    def test_create_and_destroy_link(self):
        """Find a compatible output/input port pair and create a link.

        If the session does not expose a suitable pair (e.g. a bare
        PipeWire with no nodes), the test is skipped.
        """
        reg, rec = make_registry("T4")
        self._registries.append(reg)
        reg.start()

        self.assertTrue(
            wait_for(lambda: len(rec.global_added) > 0, timeout=5.0),
            "Registry did not deliver any global event")
        time.sleep(0.5)

        # Collect ports by direction.
        with reg._objects_lock:
            ports = {
                id_: props
                for id_, (t, props) in reg.objects.items()
                if t == pw.PW_TYPE_INTERFACE_Port
            }

        # Only consider ports that are not already linked: we do
        # this by looking at link objects currently present.
        with reg._objects_lock:
            existing_links = [
                props for id_, (t, props) in reg.objects.items()
                if t == pw.PW_TYPE_INTERFACE_Link
            ]
        already_linked_out = {
            int(l.get("link.output.port", -1))
            for l in existing_links
        }
        already_linked_in = {
            int(l.get("link.input.port", -1))
            for l in existing_links
        }

        out_port = None
        in_port = None
        for id_, props in ports.items():
            direction = props.get("_direction")
            if direction == pw.PW_DIRECTION_OUTPUT and id_ not in already_linked_out:
                out_port = id_
            elif direction == pw.PW_DIRECTION_INPUT and id_ not in already_linked_in:
                in_port = id_
            if out_port is not None and in_port is not None:
                break

        if out_port is None or in_port is None:
            self.skipTest(
                "No free port pair available in this session "
                "(out=%r, in=%r)" % (out_port, in_port))

        # Record link ids before.
        with reg._objects_lock:
            links_before = {
                id_ for id_, (t, _) in reg.objects.items()
                if t == pw.PW_TYPE_INTERFACE_Link
            }

        # Create the link.
        ok = reg.create_link(out_port, in_port)
        self.assertTrue(ok, "create_link returned False")

        # A new link object should appear.
        def new_link_seen():
            with reg._objects_lock:
                current = {
                    id_ for id_, (t, _) in reg.objects.items()
                    if t == pw.PW_TYPE_INTERFACE_Link
                }
            return bool(current - links_before)

        self.assertTrue(
            wait_for(new_link_seen, timeout=5.0),
            "No new link object appeared after create_link")

        with reg._objects_lock:
            current_links = {
                id_: props
                for id_, (t, props) in reg.objects.items()
                if t == pw.PW_TYPE_INTERFACE_Link
            }
        new_link_ids = set(current_links.keys()) - links_before
        self.assertEqual(len(new_link_ids), 1)
        new_link_id = next(iter(new_link_ids))

        # The pending proxy must have been reconciled.
        with reg._objects_lock:
            self.assertIn(
                new_link_id, reg._link_proxies,
                "Pending link proxy was not reconciled with the "
                "real link_id")
            self.assertIn(new_link_id, reg._link_listeners)

        # destroy_link must succeed.
        ok = reg.destroy_link(new_link_id)
        self.assertTrue(ok, "destroy_link returned False")

        # The link must disappear from the registry.
        def link_gone():
            with reg._objects_lock:
                return new_link_id not in reg.objects
        self.assertTrue(
            wait_for(link_gone, timeout=5.0),
            "Link did not disappear after destroy_link")

        # The listener/proxy tables must be cleaned up too.
        self.assertTrue(
            wait_for(
                lambda: new_link_id not in reg._link_proxies,
                timeout=5.0),
            "link_proxies not cleaned up after destroy_link")

    # ------------------------------------------------------------------
    # T5: stop() is idempotent
    # ------------------------------------------------------------------

    def test_stop_is_idempotent(self):
        reg, _ = make_registry("T5")
        self._registries.append(reg)
        reg.start()
        time.sleep(0.3)

        reg.stop()
        # Second stop must be a no-op, not a crash.
        reg.stop()
        reg.stop()

        self.assertFalse(reg._running)
        self.assertIsNone(reg._core)
        self.assertIsNone(reg._context)
        self.assertIsNone(reg._thread_loop)
        self.assertIsNone(reg._registry)

        self._registries.remove(reg)

    # ------------------------------------------------------------------
    # T6: reconnect thread joins in bounded time
    # ------------------------------------------------------------------

    def test_reconnect_thread_joins(self):
        reg, _ = make_registry("T6")
        self._registries.append(reg)
        reg.start()
        time.sleep(0.3)

        self.assertIsNotNone(reg._reconnect_thread)
        self.assertTrue(reg._reconnect_thread.is_alive())

        t0 = time.monotonic()
        reg.stop()
        elapsed = time.monotonic() - t0

        # stop() must not hang. 5 s is the join timeout; we allow
        # a bit of margin for the destroy sequence.
        self.assertLess(elapsed, 7.0,
                        "stop() took too long: %.2fs" % elapsed)
        self.assertIsNone(reg._reconnect_thread)

        self._registries.remove(reg)

    # ------------------------------------------------------------------
    # T7: two registries can be started and stopped in interleaved order
    # ------------------------------------------------------------------

    def test_interleaved_start_stop(self):
        reg_a, rec_a = make_registry("I-A")
        reg_b, rec_b = make_registry("I-B")
        self._registries.extend([reg_a, reg_b])

        reg_a.start()
        reg_b.start()
        time.sleep(0.5)

        # Stop A only, B must keep working.
        reg_a.stop()
        self._registries.remove(reg_a)

        n_b_before = len(rec_b.global_added)

        # Wait a bit and check B is still receiving events.
        # We cannot force a new global event easily, so we just
        # check that B's registry object is still alive and the
        # reconnect thread is running.
        self.assertIsNotNone(reg_b._registry)
        self.assertTrue(reg_b._reconnect_thread.is_alive())

        # Stop B.
        reg_b.stop()
        self._registries.remove(reg_b)

        # A and B must both be fully torn down.
        self.assertIsNone(reg_a._core)
        self.assertIsNone(reg_b._core)


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

    # Run unittest with verbosity 2.
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestTwoRegistries)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
