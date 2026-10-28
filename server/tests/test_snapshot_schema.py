"""Snapshot contract: every representative record validates against
shared/schemas/snapshot.schema.json, and both sides agree on api_version."""

import json
import re
import tempfile
import unittest
from pathlib import Path

import jsonschema

from server import protocol as proto
from server.store import RecordStore

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA = json.loads((ROOT / "shared" / "schemas" / "snapshot.schema.json").read_text())
VALIDATE = jsonschema.Draft202012Validator(SCHEMA)


def T(name, **args):
    return {"name": name, "args": args}


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecordStore(path=Path(self.tmp.name) / "r.json")
        self.sess = {}

    def check(self, snap=None):
        snap = self.s.snapshot() if snap is None else snap
        errs = list(VALIDATE.iter_errors(snap))
        self.assertEqual(errs, [], [e.message for e in errs][:3])

    def test_fresh(self):
        self.check()

    def test_demo_run(self):
        self.s.start_demo_run()
        self.s.r["step_index"] = 2
        self.check()

    def test_full_bench(self):
        self.s.start_demo_run()
        self.s.r["step_index"] = 2
        self.s.apply_calls([T("mark_done", wells=["C2"])], "bench", "C2 done", "spark", self.sess)
        self.s.apply_calls([T("exclude_well", well="C8", reason="cloudy")],
                           "bench", "drop C8, cloudy", "spark", self.sess)
        self.s.apply_calls([T("record_redo", wells=["C9"], note="bubble")],
                           "bench", "redo C9, bubble", "spark", self.sess)
        self.s.apply_calls([T("add_note", target="C6", text="sticky")],
                           "bench", "note C6, sticky", "spark", self.sess)
        self.s.apply_calls([T("save_tip", step_id="3", text="go slow")],
                           "bench", "tip for step 3: go slow", "spark", self.sess)
        self.s.apply_calls([T("set_timer", minutes=5, wells=["C2"], on_done="wash")],
                           "bench", "5 minute timer for C2, wash after", "spark", self.sess)
        self.s.apply_calls([T("next_step")], "bench", "next", "spark", self.sess)
        self.check()
        self.assertTrue(self.s.r["cards"], "cards covered")
        self.assertTrue(self.s.r["timers"], "timers covered")
        self.assertTrue(self.s.r["notes"], "notes covered")

    def test_draft_present(self):
        self.s.start_demo_run()
        raw = {"title": "T", "liquids": [], "conditions": [], "steps": []}
        self.s.r["draft"] = proto.build_draft(raw, proto.evidence_from_text("Seed it."), "t.txt")
        self.check()

    def test_legacy_promotion(self):
        old = {"version": 1, "plan": {"conditions": {}, "steps": [
            {"kind": "wait", "text": "w", "wells": []}], "backups": []},
            "step_index": 0, "done": [], "excluded": {}, "redo": {},
            "notes": [], "tips": [], "timers": [], "stamps": {}, "cards": {},
            "recent": []}
        p = Path(self.tmp.name) / "old.json"
        p.write_text(json.dumps(old))
        s2 = RecordStore(path=p)
        self.assertEqual(s2.r["run"]["run_id"], "legacy")
        self.check(s2.snapshot())

    def test_archived_record(self):
        self.s.start_demo_run()
        self.s.apply_calls([T("mark_done", wells=["B2"])], "bench", "B2 done", "spark", self.sess)
        self.s._finish_locked("finished")
        docs = list((Path(self.tmp.name) / "runs").glob("run-*.json"))
        self.assertEqual(len(docs), 1)
        self.check(json.loads(docs[0].read_text())["record"])

    def test_null_replicates_normalized(self):
        plan = {"conditions": {"x": {"label": "X", "treatments": [],
                                    "replicates": None, "wells": ["A1", "A2"]}},
                "steps": [{"kind": "wait", "text": "w"}], "backups": []}
        live = proto.build_start_plan(plan)
        self.assertEqual(live["conditions"]["x"]["replicates"], 2)
        self.s._begin_locked(live, None, None)
        self.check()

    def test_schema_bites(self):
        bad = self.s.snapshot()
        del bad["version"]
        with self.assertRaises(jsonschema.ValidationError):
            VALIDATE.validate(bad)
        bad = self.s.snapshot()
        bad["done"] = "C2"
        with self.assertRaises(jsonschema.ValidationError):
            VALIDATE.validate(bad)

    def test_api_version_agrees(self):
        api_src = (ROOT / "server" / "api.py").read_text()
        app_src = (ROOT / "ios" / "Meta-Global-AI-Developer-Hackathon" /
                   "Meta-Global-AI-Developer-Hackathon" / "PipClient.swift").read_text()
        server_v = int(re.search(r"^API_VERSION = (\d+)", api_src, re.M).group(1))
        app_v = int(re.search(r"EXPECTED_API_VERSION\s*=\s*(\d+)", app_src).group(1))
        self.assertEqual(server_v, app_v)
        import server.api as api
        from fastapi.testclient import TestClient
        h = TestClient(api.app).get("/health").json()
        self.assertEqual(h["api_version"], server_v)


if __name__ == "__main__":
    unittest.main()
