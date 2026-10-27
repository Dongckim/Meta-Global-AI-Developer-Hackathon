"""Unit tests for server/grammar.py. Stdlib only. Run from repo root:

    .venv/bin/python -m unittest server.test_grammar -v
"""

import unittest

from server.grammar import (
    call_wells,
    collapse_each_timers,
    count_wells,
    expand_range,
    expand_tool_wells,
    fill_on_done,
    filter_timer_calls,
    final_pip_line,
    find_amounts,
    find_durations,
    is_failure_message,
    join_wells_ranges,
    keep_grounded_timers,
    mark_each_timers,
    match_grammar,
    needs_minutes_ask,
    normalize_text,
    parse_wells,
    relevant_plan_numbers,
    split_ask,
    strip_fillers,
    timer_set_reply,
    verbatim_span,
    where_am_i_reply,
    words_to_minutes,
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

    def test_how_many_left(self):
        st = {"step": 2, "total": 5, "step_name": "drug A",
              "done": ["C3"], "remaining": ["C2"]}
        for t in ["how many left", "what's left", "whats left", "which wells are left",
                  "how many remaining"]:
            calls, reply = match_grammar(t, st)
            self.assertEqual(calls, [{"name": "where_am_i", "args": {}}], t)
            self.assertIn("1 left: C2.", reply, t)

    def test_left_routing_safety(self):
        self.assertEqual(match_grammar("cancel the remaining timer", ST)[0][0]["name"],
                         "manage_timers")
        self.assertIsNone(match_grammar("C2 left", ST))

    def test_edit_last_note(self):
        calls, reply = match_grammar("edit last note, no bubbles actually", ST)
        self.assertEqual(calls, [{"name": "edit_note", "args": {"text": "no bubbles actually"}}])
        self.assertEqual(reply, "Note updated.")
        self.assertIsNotNone(match_grammar("edit my last note x", ST))
        self.assertIsNone(match_grammar("edit last note", ST))

    def test_cancel(self):
        calls, reply = match_grammar("cancel the wash timer", ST)
        self.assertEqual(calls, [{"name": "manage_timers",
                                  "args": {"action": "cancel", "label": "wash"}}])
        self.assertEqual(reply, "Canceling the wash timer.")
        calls, reply = match_grammar("cancel drug A timer", ST)
        self.assertEqual(calls[0]["args"], {"action": "cancel", "label": "drug A"})
        calls, reply = match_grammar("cancel the timer", ST)
        self.assertEqual(calls[0]["args"], {"action": "cancel"})
        self.assertEqual(reply, "Canceling the timer.")
        calls, _ = match_grammar("cancel timer", ST)
        self.assertEqual(calls[0]["args"], {"action": "cancel"})

    def test_extend(self):
        calls, reply = match_grammar("extend the wash timer by 10 min", ST)
        self.assertEqual(calls, [{"name": "manage_timers",
                                  "args": {"action": "extend", "label": "wash", "minutes": 10}}])
        self.assertEqual(reply, "Extending the wash timer by 10 minutes.")
        calls, _ = match_grammar("extend wash timer by five minutes", ST)
        self.assertEqual(calls[0]["args"]["minutes"], 5)
        calls, _ = match_grammar("extend the timer by half an hour", ST)
        self.assertEqual(calls[0]["args"], {"action": "extend", "minutes": 30})
        calls, _ = match_grammar("extend drug A timer by twenty five min", ST)
        self.assertEqual(calls[0]["args"]["minutes"], 25)

    def test_timer_fallthrough(self):
        for t in ["cancel wash", "cancel the wash", "extend wash timer",
                  "extend wash timer by someday min", "cancel the wash timer please"]:
            self.assertIsNone(match_grammar(t, ST), t)

    def test_words_to_minutes(self):
        self.assertEqual(words_to_minutes("5"), 5)
        self.assertEqual(words_to_minutes("2.5"), 2.5)
        self.assertEqual(words_to_minutes("five"), 5)
        self.assertEqual(words_to_minutes("twenty five"), 25)
        self.assertEqual(words_to_minutes("ninety"), 90)
        self.assertEqual(words_to_minutes("half an hour"), 30)
        self.assertIsNone(words_to_minutes("someday"))
        self.assertIsNone(words_to_minutes(""))


class FindAmountsTests(unittest.TestCase):
    def test_digits_and_words(self):
        self.assertEqual(find_amounts("timer 5"), {5})
        self.assertEqual(find_amounts("timer five minutes"), {5})
        self.assertEqual(find_amounts("twenty five minutes"), {25})
        self.assertEqual(find_amounts("timer 2.5"), {2.5})

    def test_hours(self):
        self.assertEqual(find_amounts("timer 2 hours"), {120})
        self.assertEqual(find_amounts("set timer half an hour"), {30})
        self.assertEqual(find_amounts("an hour timer"), {60})
        self.assertEqual(find_amounts("one and a half hours"), {90})

    def test_wells_excluded(self):
        self.assertEqual(find_amounts("B2 done, set a timer"), set())
        self.assertEqual(find_amounts("30 minute timer for C2-C8"), {30})
        self.assertEqual(find_amounts("set a timer"), set())


class FindDurationsTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(find_durations("incubate 30 min"), {30})
        self.assertEqual(find_durations("30분 배양"), {30})
        self.assertEqual(find_durations("rest 2 hours"), {120})
        self.assertEqual(find_durations("wait five minutes"), {5})

    def test_volumes_and_wells_ignored(self):
        self.assertEqual(find_durations("배지 100µL B2–D8"), set())
        self.assertEqual(find_durations("drug A C2–C8"), set())
        self.assertEqual(find_durations("vehicle"), set())


class RelevanceTests(unittest.TestCase):
    def test_stem_match(self):
        self.assertEqual(relevant_plan_numbers("set the incubation timer", ["incubate 30 min"]), {30})
        self.assertEqual(relevant_plan_numbers("set the wash timer", ["wash 5 min"]), {5})

    def test_no_match(self):
        self.assertEqual(relevant_plan_numbers("start the timer", ["incubate 30 min"]), set())
        self.assertEqual(relevant_plan_numbers("set a 10 minute timer", ["incubate 30 min"]), set())
        self.assertEqual(relevant_plan_numbers("", ["incubate 30 min"]), set())


class TimerGroundingTests(unittest.TestCase):
    def T(self, minutes):
        return {"name": "set_timer", "args": {"label": "x", "minutes": minutes}}

    def test_value_match(self):
        self.assertEqual(keep_grounded_timers([self.T(5)], {5}), [self.T(5)])
        self.assertEqual(keep_grounded_timers([self.T(30)], {30.0}), [self.T(30)])
        self.assertEqual(keep_grounded_timers([self.T(5)], {10}), [])
        self.assertEqual(keep_grounded_timers([self.T(None)], {5}), [])
        self.assertEqual(keep_grounded_timers([self.T(True)], {1}), [])

    def test_others_kept(self):
        calls = [self.T(5), {"name": "next_step", "args": {}}]
        self.assertEqual(keep_grounded_timers(calls, set()), [calls[1]])


class FilterTests(unittest.TestCase):
    def test_drops_ungrounded(self):
        t = {"name": "set_timer", "args": {"label": "x", "minutes": 5}}
        self.assertEqual(filter_timer_calls([t], "set a timer"), [])
        self.assertEqual(filter_timer_calls([t], "timer 5"), [t])
        self.assertEqual(filter_timer_calls([t], "set a 10 minute timer"), [])
        self.assertEqual(filter_timer_calls([t], "set a timer", {5}), [t])

    def test_keeps_non_timers(self):
        m = {"name": "mark_done", "args": {}}
        self.assertEqual(filter_timer_calls([m], "mark B2 done, start the timer"), [m])

    def test_manage_timers_kept(self):
        m = {"name": "manage_timers", "args": {"action": "cancel"}}
        self.assertEqual(filter_timer_calls([m], "cancel the timer"), [m])

    def test_multi_intent_to_spark(self):
        norm = normalize_text("C two to C eight done let's start the timer for themselves")
        self.assertIn("C2 to C8", norm)
        self.assertIsNone(match_grammar(norm, ST))  # multi-intent -> Spark
        calls = [{"name": "mark_done", "args": {"wells": ["C2"]}}]
        self.assertEqual(filter_timer_calls(calls, norm), calls)
        self.assertTrue(needs_minutes_ask(calls, norm))


class AskGateTests(unittest.TestCase):
    def test_verbs_ask(self):
        for t in ["start the timer", "set a timer", "I need a new timer", "begin the timer"]:
            self.assertTrue(needs_minutes_ask([], t), t)

    def test_no_verb_no_ask(self):
        self.assertFalse(needs_minutes_ask([], "the timer"))

    def test_surviving_call_no_ask(self):
        t = {"name": "set_timer", "args": {"minutes": 5}}
        self.assertFalse(needs_minutes_ask([t], "set a timer"))
        m = {"name": "manage_timers", "args": {"action": "cancel"}}
        self.assertFalse(needs_minutes_ask([m], "cancel the timer"))

    def test_cancel_stop_extend_never_ask(self):
        for t in ["cancel the timer", "stop the timer", "extend the timer",
                  "start a new timer but cancel the old one"]:
            self.assertFalse(needs_minutes_ask([], t), t)


class FillOnDoneTests(unittest.TestCase):
    @staticmethod
    def T(**args):
        return {"name": "set_timer", "args": {"label": "x", "minutes": 30, **args}}

    def test_after_fills(self):
        out = fill_on_done([self.T()], "thirty minute timer, wash after")
        self.assertEqual(out[0]["args"]["on_done"], "wash")

    def test_then_fills(self):
        out = fill_on_done([self.T()], "start a timer then wash")
        self.assertEqual(out[0]["args"]["on_done"], "wash")

    def test_code_beats_model(self):
        out = fill_on_done([self.T(on_done="rest")], "thirty minute timer, wash after")
        self.assertEqual(out[0]["args"]["on_done"], "wash")

    def test_model_kept_without_pattern(self):
        out = fill_on_done([self.T(on_done="rest")], "thirty minute timer")
        self.assertEqual(out[0]["args"]["on_done"], "rest")

    def test_other_calls_untouched(self):
        m = {"name": "mark_done", "args": {"wells": ["C2"]}}
        self.assertEqual(fill_on_done([m], "timer, wash after"), [m])

    def test_traps_ignored(self):
        self.assertEqual(fill_on_done([self.T()], "set a timer after lunch")[0]["args"].get("on_done"), None)
        self.assertEqual(fill_on_done([self.T()], "timer for half an hour")[0]["args"].get("on_done"), None)


class MarkEachTests(unittest.TestCase):
    @staticmethod
    def T(**args):
        return {"name": "set_timer", "args": {"label": "x", "minutes": 30, **args}}

    def test_marks_single(self):
        for t in ["thirty minute timer for each", "timer for all wells", "timer for every well"]:
            out = mark_each_timers([self.T()], t)
            self.assertTrue(out[0]["args"].get("each"), t)

    def test_no_phrase_untouched(self):
        out = mark_each_timers([self.T()], "thirty minute timer")
        self.assertNotIn("each", out[0]["args"])

    def test_fanout_collapses_then_marks(self):
        out = filter_timer_calls([self.T(), self.T()], "thirty minute timer for each well")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["args"].get("each"))

    def test_each_same_count(self):
        stq = {"pending_question": {"question": "How many minutes?", "target": "C2–C8"}}
        self.assertEqual(
            timer_set_reply([{"name": "set_timer", "args": {"minutes": 30, "each": True}}], stq),
            "7 30-minute timers for C2–C8.",
        )


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
            "7 30-minute timers for C2–C8.",
        )

    def test_float_minutes(self):
        self.assertEqual(
            timer_set_reply([{"name": "set_timer", "args": {"minutes": 2.5}}], self.STQ),
            "7 2.5-minute timers for C2–C8.",
        )

    def test_singular(self):
        stq = {"pending_question": {"question": "How many minutes?", "target": "C4"}}
        self.assertEqual(
            timer_set_reply([{"name": "set_timer", "args": {"minutes": 30}}], stq),
            "30-minute timer for C4.",
        )

    def test_count_wells(self):
        self.assertEqual(count_wells("C2–C8"), 7)
        self.assertEqual(count_wells("C2-C8"), 7)
        self.assertEqual(count_wells("C2, D2"), 2)
        self.assertEqual(count_wells("C4"), 1)

    def test_keeps_model_reply(self):
        t = [{"name": "set_timer", "args": {"minutes": 30}}]
        self.assertIsNone(timer_set_reply(t, {}))
        self.assertIsNone(timer_set_reply(t + [{"name": "next_step", "args": {}}], self.STQ))
        self.assertIsNone(timer_set_reply([{"name": "next_step", "args": {}}], self.STQ))


class ParseWellsTests(unittest.TestCase):
    def test_range(self):
        self.assertEqual(parse_wells("C2-C8"), ["C2", "C3", "C4", "C5", "C6", "C7", "C8"])

    def test_list(self):
        self.assertEqual(parse_wells("C2,C5,C7"), ["C2", "C5", "C7"])

    def test_combo_and_case(self):
        self.assertEqual(parse_wells("c2-c3, D5"), ["C2", "C3", "D5"])

    def test_invalid_dropped(self):
        self.assertEqual(parse_wells("C2,Z99"), ["C2"])
        self.assertEqual(parse_wells("C2-Z99"), [])
        self.assertEqual(parse_wells("xx"), [])
        self.assertEqual(parse_wells(""), [])

    def test_list_passthrough(self):
        self.assertEqual(parse_wells(["c2", "Z9"]), ["C2"])


class ExpandToolWellsTests(unittest.TestCase):
    def test_string_expands(self):
        out = expand_tool_wells([{"name": "mark_done", "args": {"wells": "C2-C3"}}])
        self.assertEqual(out[0]["args"]["wells"], ["C2", "C3"])

    def test_invalid_call_dropped(self):
        out = expand_tool_wells([{"name": "mark_done", "args": {"wells": "xx"}}])
        self.assertEqual(out, [])

    def test_other_tools_untouched(self):
        m = {"name": "manage_timers", "args": {"action": "list"}}
        self.assertEqual(expand_tool_wells([m]), [m])


class SplitAskTests(unittest.TestCase):
    def test_pulls_question(self):
        q, rest = split_ask([{"name": "ask", "args": {"question": "Which timer"}}])
        self.assertEqual((q, rest), ("Which timer?", []))

    def test_keeps_mark(self):
        q, rest = split_ask([{"name": "ask", "args": {"question": "Which timer?"}}])
        self.assertEqual(q, "Which timer?")

    def test_empty_ignored(self):
        m = {"name": "mark_done", "args": {}}
        q, rest = split_ask([{"name": "ask", "args": {}}, m])
        self.assertEqual((q, rest), (None, [m]))


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

    def test_remaining(self):
        st = {"step": 2, "total": 5, "step_name": "drug A",
              "done": ["C3", "C4", "C5", "C6", "C7"], "remaining": ["C2", "C8"]}
        self.assertEqual(
            where_am_i_reply(st),
            "Step 3 of 5, drug A. 5 wells done. 2 left: C2, C8.",
        )
        st["remaining"] = []
        self.assertEqual(where_am_i_reply(st), "Step 3 of 5, drug A. 5 wells done. None left.")


class VerbatimTests(unittest.TestCase):
    def test_strip_fillers(self):
        self.assertEqual(strip_fillers("um, bubbles, uh"), "bubbles")
        self.assertEqual(strip_fillers("like, I see bubbles"), "I see bubbles")
        self.assertEqual(strip_fillers("I like bubbles"), "I like bubbles")
        self.assertEqual(strip_fillers("  Bubbles! "), "Bubbles")

    def test_span_keeps_original_case(self):
        self.assertEqual(verbatim_span("note C3, Big Bubbles", "big bubbles"), "Big Bubbles")

    def test_model_prefix_dropped(self):
        self.assertEqual(verbatim_span("note C3, bubbles", "Note: bubbles here"), "bubbles")

    def test_no_match_returns_transcript(self):
        self.assertEqual(verbatim_span("note C3, um, bubbles", "totally absent"), "note C3, um, bubbles")


class CallWellsTests(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(call_wells({"name": "mark_done", "args": {"wells": ["C2", "C3"]}}), ["C2", "C3"])
        self.assertEqual(call_wells({"name": "exclude_well", "args": {"well": "C3"}}), ["C3"])
        self.assertEqual(call_wells({"name": "add_note", "args": {"target": "C5-C6"}}), ["C5", "C6"])
        self.assertEqual(call_wells({"name": "set_timer", "args": {"label": "x"}}), [])
        self.assertEqual(call_wells({"name": "add_note", "args": {"target": "wash"}}), [])


class TimerWellsExpandTests(unittest.TestCase):
    def test_string_expands(self):
        out = expand_tool_wells([{"name": "set_timer", "args": {"label": "x", "minutes": 5, "wells": "C5-C6"}}])
        self.assertEqual(out[0]["args"]["wells"], ["C5", "C6"])

    def test_invalid_falls_back(self):
        out = expand_tool_wells([{"name": "set_timer", "args": {"label": "x", "minutes": 5, "wells": "xx"}}])
        self.assertEqual(len(out), 1)
        self.assertNotIn("wells", out[0]["args"])


class JoinRangesTests(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(join_wells_ranges(["C2", "C5", "C6", "C7", "C8"]), "C2, C5–C8")
        self.assertEqual(join_wells_ranges(["C3", "C2"]), "C2–C3")
        self.assertEqual(join_wells_ranges(["C2"]), "C2")
        self.assertEqual(join_wells_ranges([]), "")
        self.assertEqual(join_wells_ranges(["xx", "C2"]), "C2")


class PipLineTests(unittest.TestCase):
    def test_failures_replace(self):
        self.assertEqual(final_pip_line([], ["I couldn't find a wash timer."], "Timers."), "I couldn't find a wash timer.")

    def test_mixed_joins(self):
        self.assertEqual(
            final_pip_line(["✓ C2 done"], ["That timer already finished."], "x"),
            "✓ C2 done, but that timer already finished.",
        )

    def test_failures_detected(self):
        self.assertTrue(is_failure_message("Which timer should I cancel? Running: x."))
        self.assertFalse(is_failure_message("✓ C2 done"))


if __name__ == "__main__":
    unittest.main()
