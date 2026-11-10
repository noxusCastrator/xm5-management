"""Enumerate before/after cuts at actual inline delta transaction boundaries.

Counts are cut scenarios, not unittest assertions or historical acceptance cases.
This is process-level simulation, not power-loss/filesystem durability testing.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_storage_recovery import Fixture, PowerCut, delta, export_card, snapshot


def perform(module, method, number=None, when=None, action=None):
    with tempfile.TemporaryDirectory(prefix="fault-") as temp:
        f = Fixture(Path(temp))
        plan = f.changed_plan()
        sources = snapshot(f.music)
        original = getattr(module, method)
        calls = []
        fired = False

        def wrapped(*args, **kwargs):
            nonlocal fired
            calls.append([str(x) for x in args[1:3]])
            selected = len(calls) == number
            if selected and when == "before":
                fired = True
                raise PowerCut()
            result = original(*args, **kwargs)
            if selected and when == "after":
                fired = True
                raise PowerCut()
            return result

        with patch.object(module, method, wrapped):
            try:
                delta.execute(plan)
            except PowerCut:
                pass
        assert snapshot(f.music) == sources, "source changed by export/cut"
        if number is None:
            return calls
        assert fired, "requested cut was not reached"
        transactions = delta.transactions(f.card)
        if not transactions:
            assert snapshot(f.card / "xm5") == f.before, "pre-activation cut changed card"
            return {"boundary": method, "call": number, "when": when, "action": action,
                    "status": "pre_activation_preserved", "pass": True}
        transaction = transactions[0]["transaction_id"]
        # Resume/rollback must use frozen stage/backup bytes, not the source drive.
        f.source.unlink()
        result = delta.recover(f.card, transaction, action=action)
        assert result["status"] in ("complete", "rolled_back"), result
        if result["status"] == "rolled_back":
            assert snapshot(f.card / "xm5") == f.before, "rollback did not restore original card bytes"
        else:
            assert f.active.read_bytes() == b"replacement ordinary bytes\n", "resume bytes differ"
        before_replay = snapshot(f.card)
        replay = delta.recover(f.card, transaction, action=action)
        assert replay["status"] == result["status"], replay
        assert snapshot(f.card) == before_replay, "replay changed file bytes"
        return {"boundary": method, "call": number, "when": when, "action": action,
                "status": result["status"], "pass": True}


import unittest

class StorageFaultBoundaries(unittest.TestCase):
    def test_resume_rollback_and_replay_across_observed_boundaries(self):
        boundaries = [(export_card, "_copy_file"), (delta, "_move"), (delta, "_save"),
                      (delta, "_atomic"), (delta, "unlink_owned")]
        count = 0
        for module, method in boundaries:
            traced = perform(module, method)
            for number in range(1, len(traced) + 1):
                for when in ("before", "after"):
                    for action in ("resume", "rollback"):
                        with self.subTest(boundary=method, call=number, when=when, action=action):
                            self.assertTrue(perform(module, method, number, when, action)["pass"])
                            count += 1
        self.assertGreaterEqual(count, 88)
