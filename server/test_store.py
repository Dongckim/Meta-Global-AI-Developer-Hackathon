"""RecordStore: single truth, apply rules, cards, plans, timers."""

import tempfile
import time
import unittest
from pathlib import Path

from server.runs import done_wells
from server.store import RecordStore


def T(name, **args):
    return {"name": name, "args": args}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = RecordStore(path=Path(self.tmp.name) / "r.json")
        self.s.start_demo_run()
        self.s.r["step_index"] = 2  # mid-run fixture, as before
        self.sess = {}

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, calls, origin="test", route="spark"):
        return self.s.apply_calls(calls, "voice", origin, route, self.sess)

    def test_seed(self):
        self.assertEqual(self.s.r["step_index"], 2)
        self.assertEqual(len(self.s.steps()), 5)
        self.assertEqual(sorted(done_wells(self.s.r)), [])

    def test_epoch_lineage(self):
        first = self.s.r["epoch"]
        self.assertTrue(first)
        # Restart on the same file keeps the epoch.
        again = RecordStore(path=Path(self.tmp.name) / "r.json")
        self.assertEqual(again.r["epoch"], first)
        # Start/end carry it (same lineage, not a reset).
        self.s._begin_locked({"conditions": {}, "steps": []}, None, None)
        self.assertEqual(self.s.r["epoch"], first)
        self.s._finish_locked("finished")
        self.assertEqual(self.s.r["epoch"], first)
        # Data reset mints a new one.
        (Path(self.tmp.name) / "r.json").unlink()
        reset = RecordStore(path=Path(self.tmp.name) / "r.json")
        self.assertNotEqual(reset.r["epoch"], first)
        self.assertEqual(reset.r["version"], 0)

    def test_mark_planned(self):
        r = self.apply([T("mark_done", wells=["C2", "C3"])])
        self.assertEqual(r["messages"], ["Nice, C2–C3 are in."])
        self.assertEqual(r["cards"], [])
        self.assertEqual(sorted(done_wells(self.s.r)), ["C2", "C3"])

    def test_done_is_per_step(self):
        # fixture is at step 3 (drug A, C2-C8); step 1 put media in every well
        self.apply([T("mark_done", wells=["C2", "C3"], step=1)])
        self.assertEqual(done_wells(self.s.r, 1), {"C2", "C3"})
        self.assertEqual(self.s.done_now(), set())  # step 3 still open
        self.assertEqual(self.s.remaining_wells()[:2], ["C2", "C3"])
        self.apply([T("mark_done", wells=["C2"])])  # no step -> current (3)
        self.assertEqual(self.s.done_now(), {"C2"})
        m = [x for x in self.s.r["done"] if x["well"] == "C2"]
        self.assertEqual([x["step"] for x in m], [1, 3])
        self.assertTrue(all(x["source"] == "voice" and x["ts"] > 0 for x in m))
        self.apply([T("mark_done", wells=["C2"])])  # repeat: no duplicate
        self.assertEqual(len([x for x in self.s.r["done"] if x["well"] == "C2"]), 2)

    def test_done_step_checks(self):
        r = self.apply([T("mark_done", wells=["C2"], step=9)])
        self.assertEqual(r["messages"], ["I couldn't mark that: there's no step 9."])
        self.assertEqual(self.s.r["done"], [])
        r = self.apply([T("mark_done", wells=["B2"], step=2)])  # B2 is in step 2's plan
        self.assertEqual(r["cards"], [])
        self.assertEqual(done_wells(self.s.r, 2), {"B2"})
        r = self.apply([T("mark_done", wells=["C2"], step=2)])  # off step 2's plan
        self.assertIn("isn't in this step", r["cards"][0]["question"])
        self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(done_wells(self.s.r, 2), {"B2", "C2"})  # card keeps the step

    def test_catch_and_confirm(self):
        r = self.apply([T("mark_done", wells=["A1"])])
        self.assertEqual(r["messages"], [])
        self.assertEqual(r["cards"][0]["question"], "Wait — A1 isn't in this step. Still log it?")
        self.assertEqual(sorted(done_wells(self.s.r)), [])
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "Nice, A1 are in.")
        self.assertEqual(sorted(done_wells(self.s.r)), ["A1"])

    def test_catch_plural_and_decline(self):
        r = self.apply([T("mark_done", wells=["A1", "A2"])])
        self.assertIn("isn't in this step. Still log it?", r["cards"][0]["question"])
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], False, "card", self.sess)
        self.assertEqual(msg, "Canceled.")
        self.assertEqual(sorted(done_wells(self.s.r)), [])

    def test_mark_invalid(self):
        r = self.apply([T("mark_done", wells=["Z9"])])
        self.assertEqual(r["messages"], ["Z9 isn't on the plate. Which well did you mean?"])
        self.assertEqual(r["cards"], [])

    def test_exclude_card_roundtrip(self):
        r = self.apply([T("exclude_well", well="C3", reason="contaminated")])
        self.assertEqual(r["cards"][0]["question"], "Drop C3?")
        self.assertEqual(r["cards"][0]["detail"], "contaminated")
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "C3 excluded (contaminated)")
        self.assertEqual(self.s.r["excluded"], {"C3": "contaminated"})

    def test_exclude_invalid(self):
        r = self.apply([T("exclude_well", well="Z9")])
        self.assertEqual(r["messages"], ["Z9 isn't on the plate. Which well did you mean?"])

    def test_redo_roundtrip(self):
        r = self.apply([T("record_redo", wells=["D7", "D8"], note="spill")])
        self.assertEqual(r["cards"][0]["question"], "Redo D7, D8?")
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "D7, D8 marked for redo (spill)")

    def test_note_text_and_provenance(self):
        r = self.apply([T("add_note", target="C3", text="BIG BUBBLES EVERYWHERE")],
                       origin="note C3, big bubbles everywhere", route="grammar")
        self.assertEqual(r["messages"], ["Noted on C3."])
        memo = self.s.r["notes"][-1]
        self.assertEqual(memo["text"], "BIG BUBBLES EVERYWHERE")
        self.assertEqual(memo["raw"], "note C3, big bubbles everywhere")
        self.assertEqual(memo["route"], "grammar")
        self.assertEqual(memo["wells"], ["C3"])
        self.assertEqual(r["info"]["saved_memo"]["text"], "BIG BUBBLES EVERYWHERE")

    def test_note_single_stopword_falls_back(self):
        r = self.apply([T("add_note", target="C3", text="a")], origin="note C3, take a look")
        self.assertEqual(self.s.r["notes"][-1]["text"], "note C3, take a look")
        self.assertEqual(self.s.describe_well("C3"),
                         "C3 is planned. Memos: note C3, take a look.")

    def test_edit_note_history(self):
        r = self.apply([T("edit_note", text="x")])
        self.assertEqual(r["messages"], ["No notes yet."])
        self.apply([T("add_note", target="C3", text="bubbles")], origin="note C3 bubbles")
        r = self.apply([T("edit_note", text="NO BUBBLES ACTUALLY")], origin="edit last note, no bubbles actually")
        self.assertEqual(r["messages"], ["Note updated."])
        memo = self.s.r["notes"][-1]
        self.assertEqual(memo["text"], "NO BUBBLES ACTUALLY")
        self.assertEqual(memo["history"], [{"text": "bubbles", "ts": memo["history"][0]["ts"]}])
        self.assertEqual(r["info"]["edited"], {"id": memo["id"], "target": "C3"})

    def test_edit_note_by_well(self):
        self.apply([T("add_note", target="C3", text="bubbles")], origin="note C3 bubbles")
        self.apply([T("add_note", target="C5", text="sticky")], origin="note C5 sticky")
        r = self.apply([T("edit_note", well="C3", text="NO BUBBLES")],
                       origin="change C3 memo, no bubbles")
        self.assertEqual(r["messages"], ["Note updated."])
        self.assertEqual(self.s.r["notes"][0]["text"], "NO BUBBLES")
        self.assertEqual(self.s.r["notes"][1]["text"], "sticky")
        self.assertEqual(len(self.s.r["notes"][0]["history"]), 1)

    def test_edit_note_well_without_note(self):
        self.apply([T("add_note", target="C3", text="bubbles")], origin="note C3 bubbles")
        r = self.apply([T("edit_note", well="C5", text="x")])
        self.assertEqual(r["messages"], ["No note on C5 yet."])
        r = self.apply([T("edit_note", well="Z9", text="x")])
        self.assertEqual(r["messages"], ["Z9 isn't on the plate. Which well did you mean?"])

    def test_manual_edit(self):
        self.apply([T("add_note", target="C3", text="bubbles")], origin="note C3 bubbles")
        mid = self.s.r["notes"][-1]["id"]
        self.assertEqual(self.s.edit_note_manual(mid, "fixed by hand"), "Note updated (C3).")
        self.assertEqual(self.s.r["notes"][-1]["text"], "fixed by hand")
        self.assertEqual(len(self.s.r["notes"][-1]["history"]), 1)
        self.assertEqual(self.s.edit_note_manual("nope", "x"), "That note is gone.")

    def test_tip_roundtrip(self):
        r = self.apply([T("save_tip", step_id="s1", text="tip text")], origin="tip: tip text")
        self.assertEqual(r["cards"][0]["question"], "Save this tip?")
        self.assertEqual(r["cards"][0]["detail"], "[s1] tip text")
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "Tip saved (s1)")

    def test_tip_attach_uses_candidate(self):
        self.sess["last_web"] = "Incubate 30 minutes. (Source: X)"
        self.sess["turns"] = [{"text": "usual incubation?", "calls": [], "messages": [],
                               "asked": {"question": "Attach to step 3?", "target": ""}}]
        r = self.apply([T("save_tip", step_id="s3", text="incubate 30 minutes")], origin="yes")
        self.assertEqual(r["cards"][0]["detail"], "[s3] incubate 30 minutes")
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "Tip saved (s3)")
        self.assertEqual(self.s.r["tips"][-1]["text"], "incubate 30 minutes")

    def test_timer_fanout_and_dup(self):
        r = self.apply([T("set_timer", label="x", minutes=30, wells=["C2", "C3"])])
        self.assertEqual(r["messages"], ["30 minutes on C2. I'll ping you.", "Timer's on: 30 minutes for C3."])
        r = self.apply([T("set_timer", label="x", minutes=30, wells=["C2"])])
        self.assertTrue(r["cards"][0]["question"].startswith("A drug A · C2 timer is already running ("))
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "Got it — 30 minutes on C2.")

    def test_timer_unscoped_and_each(self):
        r = self.apply([T("set_timer", label="x", minutes=5)])
        self.assertEqual(r["messages"], ["5 minutes. I'll ping you."])
        r = self.apply([T("set_timer", label="x", minutes=5, each=True)])
        self.assertEqual(len(self.s.r["timers"]), 8)

    def test_cancel_extend(self):
        r = self.apply([T("cancel_timer", label="wash")])
        self.assertEqual(r["messages"], ["Hmm, I don't see a wash timer. Want me to start one? No timers running."])
        self.apply([T("set_timer", label="x", minutes=5, wells=["C1"])])
        self.apply([T("set_timer", label="x", minutes=5, wells=["C10"])])
        r = self.apply([T("cancel_timer")])
        self.assertTrue(r["messages"][0].startswith("Which timer should I cancel?"))
        r = self.apply([T("cancel_timer", label="C1")])
        self.assertEqual(r["messages"], ["Okay, C1 timer's off."])
        self.assertEqual(len(self.s.r["timers"]), 1)
        r = self.apply([T("extend_timer", label="C10")])
        self.assertEqual(r["messages"], ["+5 min: drug A · C10 · 5 min (10 min)"])
        d = self.s.read_record("timers")
        self.assertEqual(len(d["timers"]), 1)

    def test_cancel_finished(self):
        self.apply([T("set_timer", label="x", minutes=0, wells=["C4"])])
        r = self.apply([T("cancel_timer", label="C4")])
        self.assertEqual(r["messages"], ["That timer already finished."])

    def test_next_card_and_last(self):
        r = self.apply([T("next_step")])
        self.assertEqual(len(r["cards"]), 1)
        self.assertTrue(r["cards"][0]["question"], "Ready for step 4?")
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertTrue(msg.startswith("Next: "))
        self.s.r["step_index"] = 4
        r = self.apply([T("next_step")])
        self.assertEqual(r["messages"], ["Already at the last step"])

    def test_describe_well(self):
        self.apply([T("add_note", target="C6", text="sticky")], origin="note C6, sticky")
        self.assertEqual(self.s.describe_well("C6"), "C6 is planned. Memos: sticky.")
        self.assertEqual(self.s.describe_well("C9"), "C9 is empty.")
        r = self.apply([T("exclude_well", well="C3", reason="dirty")])
        self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(self.s.describe_well("C3"), "C3 is excluded (dirty).")
        r = self.apply([T("record_redo", wells=["C9"], note="spill")])
        self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(self.s.describe_well("C9"), "C9 is marked for redo (spill).")

    def test_model_state_summary_turns(self):
        sess = {"chained": False,
                "turns": [{"text": "C2 done", "calls": ["mark_done[C2]"], "messages": ["✓ C2 done"]},
                          {"text": "yes", "calls": [], "messages": ["C3 excluded"]}] * 4}
        st = self.s.model_state(sess)
        self.assertEqual(len(st["recent_turns"]), 8)
        self.assertTrue(st["recent_turns"][0].startswith("C2 done → mark_done[C2]: ✓ C2 done"))
        self.assertIn("recent_turns", self.s.model_state({"chained": True, "turns": sess["turns"]}))
        self.assertNotIn("recent_turns", self.s.model_state({}))

    def test_where_and_show(self):
        r = self.apply([T("where_am_i")])
        self.assertTrue(r["messages"][0].startswith("Step 3 of 5"))
        r = self.apply([T("show", view="plate", target="C")])
        self.assertEqual(r["messages"], ["Viewing: plate C"])
        r = self.apply([T("open", target="importer")])
        self.assertEqual(r["ui"], [{"action": "import"}])
        self.assertEqual(r["messages"], ["Opening the importer."])

    def test_redo_needs_empty_well(self):
        self.s.r["done"] = ["C9"]
        r = self.apply([T("record_redo", wells=["C9"], note="bubble")])
        self.assertEqual(r["messages"], ["I couldn't mark C9 for redo: already done."])
        self.assertEqual(r["cards"], [])
        r = self.apply([T("record_redo", wells=["C4"], note="bubble")])
        self.assertEqual(len(r["cards"]), 1)
        self.assertEqual(r["cards"][0]["question"], "Redo C4?")

    def test_redo_busy_reasons(self):
        self.s.r["excluded"]["D6"] = "x"
        self.s.r["redo"]["D7"] = "x"
        self.s.r["timers"].append({
            "id": "t", "label": "L", "minutes": 30, "on_done": None,
            "started_at": time.time(), "step": "s", "wells": "D5",
            "warned": False, "notified": False,
        })
        for well, reason in (("D6", "already excluded"), ("D7", "already marked for redo"),
                             ("D5", "has a running timer")):
            r = self.apply([T("record_redo", wells=[well])])
            self.assertEqual(r["messages"], [f"I couldn't mark {well} for redo: {reason}."])

    def test_redo_mixed_free_and_busy(self):
        self.s.r["done"] = ["C9"]
        r = self.apply([T("record_redo", wells=["C4", "C9"])])
        self.assertEqual(r["messages"], ["I couldn't mark C9 for redo: already done."])
        self.assertEqual(r["cards"][0]["question"], "Redo C4?")

    def test_redo_confirms_into_affinity_group(self):
        r = self.apply([T("record_redo", wells=["C9"], note="backup")])
        self.assertNotIn("C9", self.s.r["plan"]["conditions"]["drug_A"]["wells"])
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "C9 marked for redo (backup)")
        self.assertIn("C9", self.s.r["plan"]["conditions"]["drug_A"]["wells"])
        self.assertNotIn("C9", self.s.find_free_wells("C3", 12))
        r = self.apply([T("mark_done", wells=["C9"])])
        self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertIn("Drug A 1/5", self.s.model_state(self.sess)["conditions"])

    def test_find_free_order(self):
        self.assertEqual(self.s.find_free_wells("C3", 6),
                         ["C9", "C10", "C11", "C12", "C1", "B9"])

    def test_find_free_skips_occupied(self):
        self.s.r["done"] = ["C9"]
        self.s.r["excluded"]["C10"] = "x"
        self.assertEqual(self.s.find_free_wells("C3", 2), ["C11", "C12"])

    def test_find_free_backups_first(self):
        self.assertEqual(self.s.r["plan"].get("backups"), [])
        self.s.r["plan"]["backups"] = ["D12", "Z9", "C9"]
        self.assertEqual(self.s.find_free_wells("C3", 3), ["D12", "C9", "C10"])
        self.s.r["done"] = ["D12"]
        self.assertEqual(self.s.find_free_wells("C3", 2), ["C9", "C10"])

    def test_find_free_count_clamp(self):
        self.assertEqual(len(self.s.find_free_wells("C3", 99)), 12)
        self.assertEqual(len(self.s.find_free_wells("C3", "x")), 3)
        self.assertEqual(self.s.find_free_wells("c3", 1), ["C9"])

    def test_multi_card_turn_queues_in_order(self):
        r = self.apply([T("exclude_well", well="C3", reason="bubble"),
                        T("record_redo", wells=["C4"], note="backup")])
        self.assertEqual([c["question"] for c in r["cards"]], ["Drop C3?", "Redo C4?"])

    def test_drop_reasons_carry_raw_args(self):
        r = self.apply([T("add_note", target="C3", text=5)])
        d = r["info"]["drop_reasons"][0]
        self.assertEqual(d["args"], {"target": "C3", "text": 5})
        r = self.apply([T("nope", x=1)])
        d = r["info"]["drop_reasons"][0]
        self.assertEqual((d["call"], d["args"], d["reason"]), ("nope", {"x": 1}, "unknown tool"))

    def test_user_text_names_no_tool(self):
        tools = ["mark_done", "exclude_well", "record_redo", "add_note", "save_tip",
                 "set_timer", "manage_timers", "where_am_i", "next_step", "show",
                 "ask", "make_robot", "read_record", "find_free_wells", "answer",
                 "propose_plan"]
        r = self.apply([
            T("mark_done", wells=["Z9"]),
            T("exclude_well", well="Z9"),
            T("record_redo", wells=["C9"]),
            T("add_note", target="C3", text=5),
            T("save_tip", step_id="s1", text=5),
            T("set_timer", label="x"),
            T("manage_timers", action="frobnicate"),
            T("read_record", kind="bogus"),
            T("find_free_wells", near="Z9"),
            T("answer", text="  "),
            T("nope"),
        ])
        seen = " ".join(r["messages"] + [c["question"] for c in r["cards"]]
                        + [d["reason"] for d in r["info"]["drop_reasons"]])
        for t in tools:
            self.assertNotIn(t, seen)

    def test_read_record_kinds(self):
        self.apply([T("add_note", target="C6", text="sticky")], origin="note C6, sticky")
        d = self.s.read_record("wells", "C6")
        self.assertEqual(d["wells"][0]["memos"], ["sticky"])
        d = self.s.read_record("notes", "C6")
        self.assertEqual([(m["target"], m["text"]) for m in d["notes"]], [("C6", "sticky")])
        d = self.s.read_record("notes", "drug_A")
        self.assertTrue(all(m["target"].startswith("C") for m in d["notes"]))
        d = self.s.read_record("steps", "current")
        self.assertEqual(len(d["steps"]), 1)
        self.assertTrue(d["steps"][0]["current"])
        d = self.s.read_record("timers", "")
        self.assertEqual(d["timers"], [])
        self.assertEqual(d["run"], (self.s.r.get("run") or {}).get("run_id"))
        d = self.s.read_record("history", "", 2)
        self.assertTrue(len(d["history"]) <= 2)
        d = self.s.read_record("wells", "C2-C8", 2)
        self.assertEqual(len(d["wells"]), 2)
        d = self.s.read_record("wells", "bogus")
        self.assertEqual(d["wells"], [])
        self.assertIn("error", self.s.read_record("bogus", ""))
        r = self.apply([T("read_record", kind="wells", filter="C6")])
        self.assertEqual(r["messages"], [])

    def test_source_defaults_to_this_protocol(self):
        pid = self.s.r["run"]["protocol_id"]
        r = self.apply([T("navigate", screen="protocol_source")])  # "open the original"
        self.assertEqual(r["messages"], ["Here's the original."])
        self.assertEqual(self.s.r["view"]["current"], {"screen": "protocol_source", "target": pid})

    def test_navigate_open_back(self):
        r = self.apply([T("navigate", screen="robot_preview")])
        self.assertEqual(r["messages"], ["Here's the robot preview."])  # registry result line
        self.assertEqual(r["ui"], [])
        self.assertEqual(self.s.r["view"]["current"],
                         {"screen": "robot_preview", "target": ""})
        r = self.apply([T("open", target="C5")])
        self.assertEqual(r["messages"], ["Opened C5."])
        self.assertEqual(self.s.r["view"]["sheet"],
                         {"name": "well_card", "target": "C5"})
        self.assertEqual(r["ui"], [{"action": "flash", "wells": ["C5"]}])
        r = self.apply([T("back")])
        self.assertEqual(r["messages"], ["Closed."])
        self.assertEqual(self.s.r["view"]["sheet"], {"name": "", "target": ""})
        r = self.apply([T("back")])
        self.assertEqual(r["messages"], ["Back to Bench."])
        self.assertEqual(self.s.r["view"]["current"]["screen"], "main")
        self.assertEqual(self.s.r["view"]["stack"], [])

    def test_ui_failures(self):
        r = self.apply([T("navigate", screen="pod bay")])
        self.assertEqual(r["messages"], ["I couldn't open the pod bay screen: no such screen."])
        self.assertEqual(r["ui"], [])
        r = self.apply([T("open", target="Z9")])
        self.assertEqual(r["messages"], ["I couldn't open Z9: not a plate well or screen."])
        r = self.apply([T("open")])
        self.assertEqual(r["messages"], ["I couldn't open that: no target given."])
        r = self.apply([T("back")])
        self.assertEqual(r["messages"], ["Already on the main screen."])
        self.assertEqual(r["ui"], [])

    def test_model_calls_never_resolve_cards(self):
        r = self.apply([T("exclude_well", well="C3")])
        before = set(self.s.r["cards"])
        self.assertTrue(before)
        self.apply([
            T("mark_done", wells=["C2"]), T("exclude_well", well="C4"),
            T("record_redo", wells=["C9"]), T("add_note", target="C2", text="x"),
            T("save_tip", step_id="s", text="x"), T("set_timer", label="x", minutes=1),
            T("cancel_timer", label="zzz"), T("where_am_i"), T("next_step"),
            T("show", view="plate"), T("make_robot"), T("read_record", kind="wells", filter="C2"),
            T("read_record", kind="free_wells", filter="C2"),
            T("navigate", screen="main"), T("open", target="C5"), T("back"),
            {"name": "answer_card", "args": {"card_id": next(iter(before)), "ok": True},
             "from_model": True},
        ], origin="x")
        self.assertTrue(before <= set(self.s.r["cards"]))

    def test_propose_plan_is_unknown_now(self):
        r = self.apply([T("propose_plan", steps=[{"label": "x", "calls": []}])])
        d = r["info"]["drop_reasons"][0]
        self.assertEqual((d["call"], d["reason"]), ("propose_plan", "unknown tool"))

    def test_confirm_gone(self):
        self.assertEqual(self.s.confirm("nope", True, "card", self.sess)[0], "That card is gone.")

    def _expired_timer(self, wid, label, ago=330, minutes=5):
        self.s.r["timers"].append({
            "id": wid, "label": label, "minutes": minutes, "on_done": None,
            "started_at": time.time() - ago, "step": "drug A", "wells": wid,
            "warned": True, "notified": False,
        })

    def test_merge_simultaneous_done(self):
        self._expired_timer("C2", "drug A · C2 · 5 min")
        self._expired_timer("C3", "drug A · C3 · 5 min")
        evs = self.s.check_timers(time.time())
        self.assertEqual([e["kind"] for e in evs], ["timer_done", "timer_done"])
        cards = [c for c in self.s.r["cards"].values() if c["kind"] == "suggest_next"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["question"], "Ready for step 4?")

    def test_suggest_fold_in(self):
        self._expired_timer("C2", "drug A · C2 · 5 min")
        self.s.check_timers(time.time())
        self._expired_timer("C5", "drug A · C5 · 5 min")
        self._expired_timer("C6", "drug A · C6 · 5 min")
        self.s.check_timers(time.time())
        cards = [c for c in self.s.r["cards"].values() if c["kind"] == "suggest_next"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["question"], "Ready for step 4?")

    def test_stale_step_cards_dropped(self):
        self._expired_timer("C2", "drug A · C2 · 5 min")
        self.s.check_timers(time.time())
        r = self.apply([T("next_step")])
        msg, _, _ = self.s.confirm(r["cards"][0]["id"], True, "card", self.sess)
        self.assertTrue(msg.startswith("Next: "))
        kinds = [c["kind"] for c in self.s.r["cards"].values()]
        self.assertNotIn("suggest_next", kinds)
        self.assertNotIn("next_step", kinds)

    def test_concurrent_expiry_and_note(self):
        import threading
        self._expired_timer("C2", "drug A · C2 · 5 min")
        self._expired_timer("C3", "drug A · C3 · 5 min")
        errs: list = []
        results: list = []

        def expire():
            try:
                for _ in range(5):
                    self.s.check_timers(time.time())
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        def note(i):
            try:
                results.append(self.s.apply_calls(
                    [{"name": "add_note", "args": {"target": "C5", "text": f"note {i}"},
                      "call_id": f"n{i}"}],
                    "voice", f"note C5, note {i}", "spark", {}))
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads = [threading.Thread(target=expire) for _ in range(2)]
        threads += [threading.Thread(target=note, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(sorted(m["text"] for m in self.s.r["notes"]),
                         ["note 0", "note 1", "note 2", "note 3"])
        for r in results:
            self.assertEqual(r["messages"], ["Noted on C5."])
        cards = [c for c in self.s.r["cards"].values() if c["kind"] == "suggest_next"]
        self.assertEqual(len(cards), 1)

    def test_outcomes_and_card_call_id(self):
        r = self.apply([{"name": "mark_done", "args": {"wells": ["C2"]}, "call_id": "c1"},
                        {"name": "exclude_well", "args": {"well": "C3"}, "call_id": "c2"}])
        self.assertEqual(len(r["outcomes"]), 2)
        self.assertEqual(r["outcomes"][0]["messages"], ["Nice, C2 are in."])
        self.assertEqual(r["outcomes"][0]["cards"], [])
        self.assertEqual(r["outcomes"][1]["cards"][0]["payload"]["_call_id"], "c2")
        msg, _, card = self.s.confirm(r["outcomes"][1]["cards"][0]["id"], True, "card", self.sess)
        self.assertEqual(msg, "C3 excluded")
        self.assertEqual(card["payload"]["_call_id"], "c2")

    def test_check_timers_warn_done_purge(self):
        now = time.time()
        self.s.r["timers"].append({"id": "a", "label": "drug A · C2 · 10 min", "minutes": 10,
                                   "on_done": "wash", "started_at": now - 6 * 60, "step": "drug A",
                                   "wells": "C2", "warned": False, "notified": False})
        self.s.r["timers"].append({"id": "b", "label": "drug A · C3 · 1 min", "minutes": 1,
                                   "on_done": None, "started_at": now - 70, "step": "drug A",
                                   "wells": "C3", "warned": True, "notified": False})
        self.s.r["timers"].append({"id": "c", "label": "old", "minutes": 1,
                                   "on_done": None, "started_at": now - 500, "step": "s",
                                   "wells": "", "warned": True, "notified": True})
        evs = self.s.check_timers(now)
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds, ["timer_warning", "timer_done"])
        self.assertEqual(evs[1]["message"], "Heads up! 1 minutes are up on C3.")
        self.assertNotIn("card", evs[1])
        cards = [c for c in self.s.r["cards"].values() if c["kind"] == "suggest_next"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["question"], "Ready for step 4?")
        self.assertEqual(sorted(t["id"] for t in self.s.r["timers"]), ["a", "b"])

    def test_plan_times(self):
        self.assertEqual(self.s.plan_times(), {4: [30]})
        st = self.s.model_state({})
        self.assertEqual(st["plan_times"], "5: 30m")
        self.assertEqual(st["step"], "3/5 drug A C2–C8")

    def test_recent_and_prompt_state(self):
        self.apply([T("mark_done", wells=["C2", "C3"])], route="grammar")
        st = self.s.prompt_state({})
        self.assertEqual(st["recent"], [{"route": "grammar", "action": "mark_done",
                                         "wells": ["C2", "C3"], "ts": st["recent"][0]["ts"]}])
        for k in ("step", "total", "step_name", "done", "excluded", "step_wells",
                  "remaining", "timers", "recent"):
            self.assertIn(k, st)
        sess = {"turns": [{"text": "t", "calls": [], "messages": [],
                            "asked": {"question": "Q?", "target": "C2"}}]}
        self.assertEqual(self.s.prompt_state(sess)["last_asked"],
                         {"question": "Q?", "target": "C2"})
        self.assertNotIn("last_asked", self.s.prompt_state({"turns": []}))

    def test_persistence_and_broadcast(self):
        seen = []
        self.s.subscribe(seen.append)
        self.apply([T("mark_done", wells=["C2"])])
        self.assertTrue(any(m.get("kind") == "snapshot" for m in seen))
        again = RecordStore(path=Path(self.tmp.name) / "r.json")
        self.assertEqual(sorted(done_wells(again.r)), ["C2"])


if __name__ == "__main__":
    unittest.main()
