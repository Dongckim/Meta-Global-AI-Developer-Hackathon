"""Unit tests for server/grammar.py. Stdlib only. Run from repo root:

    .venv/bin/python -m unittest server.test_grammar -v
"""

import unittest

from server.grammar import (
    collapse_each_timers,
    drop_ungrounded_timers,
    expand_range,
    honest_reply,
    match_grammar,
    normalize_text,
    timer_followup,
    timer_set_reply,
    where_am_i_reply,
)

ST = {"step": 2, "total": 5, "step_name": "drug A", "done": []}


class NormalizeTests(unittest.TestCase):
    def test_letters_and_digits(self):
        self.assertEqual(normalize_text("sea two done"), "C2 done")
        self.assertEqual(normalize_text("bee for done"), "B4 done")
        self.assertEqual(normalize_text("dee ate done"), "D8 done")
        self.assertEqual(normalize_text("she won done"), "C1 done")
        self.assertEqual(normalize_text("C too done"), "C2 done")

    def test_range_fill(self):
        self.assertEqual(normalize_text("C2 to 8 done"), "C2 to C8 done")
        self.assertEqual(normalize_text("C2 through 8 done"), "C2 through C8 done")
        self.assertEqual(normalize_text("C2 to five done"), "C2 to C5 done")

    def test_range_words_kept(self):
        self.assertEqual(normalize_text("B2 to C3 done"), "B2 to C3 done")
        self.assertEqual(normalize_text("see to done"), "C2 done")  # "to" w/o range = 2

    def test_verbs_kept(self):
        self.assertEqual(normalize_text("be done"), "be done")
        self.assertEqual(normalize_text("ready for B2"), "ready for B2")

    def test_case_and_punct_untouched(self):
        self.assertEqual(normalize_text("See Two Done."), "C2 Done.")
        self.assertEqual(normalize_text("NEXT!"), "NEXT!")

    def test_double_to(self):
        self.assertEqual(normalize_text("C to to eight done"), "C2 to C8 done")
        calls, _ = match_grammar(normalize_text("C to to eight done"), ST)
        self.assertEqual(
            calls[0]["args"]["wells"], ["C2", "C3", "C4", "C5", "C6", "C7", "C8"]
        )

    def test_free_text_verbatim(self):
        self.assertEqual(normalize_text("Note C3, I see bubbles"), "Note C3, I see bubbles")
        self.assertEqual(
            normalize_text("exclude C3 because I see bubbles"), "exclude C3 because I see bubbles"
        )
        self.assertIsNone(match_grammar(normalize_text("I see bubbles in C3"), ST))

    def test_be_stays_verb(self):
        self.assertEqual(normalize_text("Timer should be 2 hours"), "Timer should be 2 hours")


class ExpandTests(unittest.TestCase):
    def test_range(self):
        self.assertEqual(expand_range("C2", "C8"), ["C2", "C3", "C4", "C5", "C6", "C7", "C8"])

    def test_reversed_sorts(self):
        self.assertEqual(expand_range("C8", "C2")[0], "C2")
        self.assertEqual(len(expand_range("C8", "C2")), 7)

    def test_single(self):
        self.assertEqual(expand_range("B2"), ["B2"])
        self.assertEqual(expand_range("H12"), ["H12"])

    def test_invalid_falls_through(self):
        self.assertIsNone(expand_range("B2", "C3"))  # cross-row -> Spark
        self.assertIsNone(expand_range("B13"))
        self.assertIsNone(expand_range("A0"))
        self.assertIsNone(expand_range("Z1"))


class GrammarTests(unittest.TestCase):
    def test_spec_pipeline(self):
        calls, reply = match_grammar(normalize_text("see two to eight done"), ST)
        wells = calls[0]["args"]["wells"]
        self.assertIn("C2", wells)
        self.assertIn("C8", wells)
        self.assertEqual(len(wells), 7)
        self.assertEqual(calls[0]["name"], "mark_done")

    def test_done(self):
        calls, reply = match_grammar("B2 done", ST)
        self.assertEqual(calls, [{"name": "mark_done", "args": {"wells": ["B2"]}}])
        self.assertEqual(reply, "B2 done.")

    def test_next(self):
        calls, reply = match_grammar("next", ST)
        self.assertEqual(calls[0]["name"], "next_step")

    def test_where(self):
        calls, reply = match_grammar("where am i", ST)
        self.assertEqual(calls[0]["name"], "where_am_i")
        self.assertEqual(reply, "Step 3 of 5, drug A. 0 wells done.")

    def test_exclude(self):
        calls, reply = match_grammar("exclude C3", ST)
        self.assertEqual(calls, [{"name": "exclude_well", "args": {"well": "C3"}}])
        self.assertEqual(reply, "Exclude C3?")

    def test_exclude_reason(self):
        calls, reply = match_grammar("exclude C3, contaminated", ST)
        self.assertEqual(calls[0]["args"], {"well": "C3", "reason": "contaminated"})
        self.assertEqual(reply, "Exclude C3?")
        calls, _ = match_grammar("exclude C3 Because Spilled", ST)
        self.assertEqual(calls[0]["args"], {"well": "C3", "reason": "Because Spilled"})

    def test_note(self):
        calls, reply = match_grammar("note C3, I see bubbles", ST)
        self.assertEqual(calls[0]["args"], {"target": "C3", "text": "I see bubbles"})
        self.assertEqual(reply, "Noted for C3.")
        self.assertIsNone(match_grammar("note C3", ST))
        self.assertIsNone(match_grammar("note the time", ST))

    def test_trailing_punct(self):
        calls, _ = match_grammar("B2 done.", ST)
        self.assertEqual(calls[0]["args"]["wells"], ["B2"])
        self.assertIsNotNone(match_grammar("next!", ST))
        self.assertIsNotNone(match_grammar("where am i?", ST))
        self.assertIsNotNone(match_grammar("exclude C3.", ST))

    def test_punct_inside(self):
        calls, _ = match_grammar("note C3: check this", ST)
        self.assertEqual(calls[0]["args"], {"target": "C3", "text": "check this"})
        calls, _ = match_grammar("exclude C3: contaminated!", ST)
        self.assertEqual(calls[0]["args"], {"well": "C3", "reason": "contaminated"})
        calls, _ = match_grammar("note C3, X, Y", ST)
        self.assertEqual(calls[0]["args"]["text"], "X, Y")
        calls, _ = match_grammar("B2, done", ST)
        self.assertEqual(calls[0]["args"]["wells"], ["B2"])

    def test_fallthrough_to_spark(self):
        for t in ["do the thing", "hello", "B2 to C3 done", "B13 done", "please B2 done"]:
            self.assertIsNone(match_grammar(t, ST), t)


class TimerGroundingTests(unittest.TestCase):
    T5 = {"name": "set_timer", "args": {"label": "x", "minutes": 5}}

    def test_digits_ground(self):
        self.assertEqual(drop_ungrounded_timers([self.T5], "timer 5"), [self.T5])
        self.assertEqual(drop_ungrounded_timers([self.T5], "timer 2.5 hours"), [self.T5])

    def test_words_ground(self):
        self.assertEqual(drop_ungrounded_timers([self.T5], "timer five minutes"), [self.T5])
        self.assertEqual(drop_ungrounded_timers([self.T5], "set timer half an hour"), [self.T5])
        self.assertEqual(drop_ungrounded_timers([self.T5], "timer ninety minutes"), [self.T5])

    def test_bare_timer_dropped(self):
        self.assertEqual(drop_ungrounded_timers([self.T5], "set a timer"), [])
        self.assertEqual(drop_ungrounded_timers([self.T5], "timer please"), [])
        self.assertEqual(drop_ungrounded_timers([self.T5], "someone set a timer"), [])

    def test_others_kept(self):
        calls = [self.T5, {"name": "next_step", "args": {}}]
        self.assertEqual(drop_ungrounded_timers(calls, "next"), [calls[1]])

    def test_well_digits_dont_ground(self):
        self.assertEqual(drop_ungrounded_timers([self.T5], "B2 done, set a timer"), [])


class TimerFollowupTests(unittest.TestCase):
    def test_strips_and_asks(self):
        calls, reply = timer_followup(
            [{"name": "mark_done", "args": {}}], "B2 done. Timer started.", "mark B2 done, start the timer"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(reply, "B2 done. How many minutes for the timer?")

    def test_empty_becomes_question(self):
        calls, reply = timer_followup([], "Timer started.", "set a timer")
        self.assertEqual((calls, reply), ([], "How many minutes for the timer?"))

    def test_surviving_timer_keeps_reply(self):
        t = {"name": "set_timer", "args": {"minutes": 5}}
        self.assertEqual(timer_followup([t], "Timer started.", "timer 5"), ([t], "Timer started."))

    def test_manage_timers_counts(self):
        m = {"name": "manage_timers", "args": {"action": "cancel"}}
        self.assertEqual(timer_followup([m], "Timer canceled.", "cancel the timer"), ([m], "Timer canceled."))

    def test_no_timer_mention_untouched(self):
        self.assertEqual(timer_followup([], "Which well?", "huh"), ([], "Which well?"))

    def test_question_kept(self):
        calls, reply = timer_followup([], "Timer started. For how long?", "set a timer")
        self.assertEqual(reply, "For how long?")

    def test_timer_plus_wells_sentence(self):
        norm = normalize_text("C two to C eight done let's start the timer for themselves")
        self.assertIn("C2 to C8", norm)
        self.assertIsNone(match_grammar(norm, ST))  # multi-intent -> Spark
        calls = [{"name": "mark_done", "args": {"wells": ["C2"]}}]
        kept, fixed = timer_followup(calls, "C2 to C8 done. Timer started.", norm)
        self.assertEqual(kept, calls)
        self.assertEqual(fixed, "C2 to C8 done. How many minutes for the timer?")


class CollapseTests(unittest.TestCase):
    @staticmethod
    def T(m):
        return {"name": "set_timer", "args": {"label": "x", "minutes": m}}

    def test_for_each_collapses(self):
        calls = [self.T(30), self.T(30), {"name": "mark_done", "args": {}}]
        out = collapse_each_timers(calls, "thirty minute timer for each")
        self.assertEqual(len([c for c in out if c["name"] == "set_timer"]), 1)
        self.assertEqual(len(out), 2)

    def test_without_phrase_keeps(self):
        calls = [self.T(5), self.T(10)]
        self.assertEqual(collapse_each_timers(calls, "two timers"), calls)

    def test_single_untouched(self):
        self.assertEqual(collapse_each_timers([self.T(30)], "timer for each"), [self.T(30)])


class TimerSetReplyTests(unittest.TestCase):
    STQ = {"pending_question": {"question": "How many minutes for the timer?", "target": "C2–C8"}}

    def test_canonical(self):
        self.assertEqual(
            timer_set_reply([{"name": "set_timer", "args": {"minutes": 30}}], self.STQ),
            "30-minute timer for C2–C8.",
        )

    def test_float_minutes(self):
        self.assertEqual(
            timer_set_reply([{"name": "set_timer", "args": {"minutes": 2.5}}], self.STQ),
            "2.5-minute timer for C2–C8.",
        )

    def test_keeps_model_reply(self):
        t = [{"name": "set_timer", "args": {"minutes": 30}}]
        self.assertIsNone(timer_set_reply(t, {}))
        self.assertIsNone(timer_set_reply(t + [{"name": "next_step", "args": {}}], self.STQ))
        self.assertIsNone(timer_set_reply([{"name": "next_step", "args": {}}], self.STQ))


class HonestTests(unittest.TestCase):
    def test_lying_reply_replaced(self):
        self.assertEqual(honest_reply([], "B2 done."), "I didn't record anything.")
        self.assertEqual(honest_reply([], "C3 excluded, noted."), "I didn't record anything.")
        self.assertEqual(honest_reply(None, "Timer started."), "I didn't record anything.")

    def test_honest_replies_kept(self):
        self.assertEqual(honest_reply([], "Which well?"), "Which well?")
        self.assertEqual(honest_reply([], "Mark B2 done?"), "Mark B2 done?")
        self.assertEqual(honest_reply([], ""), "")
        self.assertEqual(honest_reply([{"name": "x"}], "B2 done."), "B2 done.")


class WhereAmITests(unittest.TestCase):
    def test_full_state(self):
        self.assertEqual(
            where_am_i_reply({"step": 2, "total": 5, "step_name": "drug A", "done": []}),
            "Step 3 of 5, drug A. 0 wells done.",
        )

    def test_partial_state(self):
        self.assertEqual(where_am_i_reply({"step": 0}), "Step 1. 0 wells done.")
        self.assertEqual(
            where_am_i_reply({"step": 1, "total": 5, "done": ["B2", "B3"]}),
            "Step 2 of 5. 2 wells done.",
        )


if __name__ == "__main__":
    unittest.main()
