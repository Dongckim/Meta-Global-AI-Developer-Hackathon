import AVFoundation
import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct PipTests {
    static func snapshot(version: Int = 3) -> RecordSnapshot {
        let json = """
        {"version": \(version),
         "plan": {"conditions": {"drug_A": {"label": "Drug A", "treatments": ["Drug A"],
                                            "replicates": 2, "wells": ["C2", "C3"]}},
                  "steps": [{"kind": "pipette", "text": "s1", "minutes": null, "temp_c": null,
                             "tip": null, "caution": null, "pipette": null, "wells": ["B2"]},
                            {"kind": "pipette", "text": "s2", "minutes": null, "temp_c": null,
                             "tip": null, "caution": null, "pipette": null, "wells": ["C2", "C3"]}]}},
         "step_index": 1, "done": ["C2"], "excluded": {}, "redo": {},
         "notes": [{"id": "m1", "target": "C2", "text": "bubbles", "ts": 1.0, "history": []}],
         "tips": [], "timers": [{"id": "t1", "label": "s2 · C2 · 30 min", "minutes": 30,
           "on_done": null, "started_at": 1000.0, "step": "s2", "wells": "C2"}],
         "stamps": {"C2": {"source": "voice", "ts": 1.0}},
         "cards": {"c1": {"id": "c1", "kind": "confirm_exclude", "question": "Exclude C3?",
                          "detail": null, "ts": 2.0},
                  "c2": {"id": "c2", "kind": "confirm_redo", "question": "Redo C4?",
                          "detail": "backup", "ts": 3.0}}}
        """
        return try! JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8))
    }

    @Test @MainActor func syncAppliesSnapshot() {
        let lab = LabState()
        lab.sync(Self.snapshot())
        #expect(lab.version == 3)
        #expect(lab.done == ["C2"])
        #expect(lab.steps == ["s1", "s2"])
        #expect(lab.stepIndex == 1)
        #expect(lab.timers.map(\.label) == ["s2 · C2 · 30 min"])
        #expect(lab.cards.map(\.question) == ["Exclude C3?", "Redo C4?"])
    }

    @Test @MainActor func olderSnapshotIgnored() {
        let lab = LabState()
        lab.sync(Self.snapshot(version: 3))
        lab.sync(Self.snapshot(version: 1))
        #expect(lab.version == 3)
    }

    @Test @MainActor func renderDerives() {
        let lab = LabState()
        lab.sync(Self.snapshot())
        #expect(lab.conditionProgress().first { $0.key == "drug_A" }?.have == 1)
        #expect(lab.conditionOf("C2") == "drug_A")
        #expect(lab.currentRange == "C2–C3")
        #expect(lab.hasNote("C2"))
        #expect(lab.memos(for: "C2").map(\.text) == ["bubbles"])
        #expect(lab.wellSource["C2"] == "voice")
    }

    @Test @MainActor func redoWellCountsInGroup() {
        let json = """
        {"version": 4,
         "plan": {"conditions": {"drug_A": {"label": "Drug A", "treatments": ["Drug A"],
                                            "replicates": 2, "wells": ["C2", "C3", "C9"]}}, "steps": []},
         "step_index": 0, "done": ["C9"], "excluded": {}, "redo": {"C9": "backup"},
         "notes": [], "tips": [], "timers": [], "stamps": {}, "cards": {}}
        """
        let lab = LabState()
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8)))
        #expect(lab.conditionProgress().first { $0.key == "drug_A" }?.have == 1)
    }

    @Test @MainActor func timerVisibility() {
        let lab = LabState()
        lab.sync(Self.snapshot())
        #expect(lab.visibleTimers(now: Date(timeIntervalSince1970: 1100)).count == 1)
        #expect(lab.visibleTimers(now: Date(timeIntervalSince1970: 100000)).isEmpty)
    }

    @Test func voiceRankPrefersAvaPremium() {
        #expect(SpeechBus.rank(name: "Ava", quality: .premium) == 0)
        #expect(SpeechBus.rank(name: "Ava", quality: .enhanced) == 1)
        #expect(SpeechBus.rank(name: "Samantha", quality: .premium) == 2)
        #expect(SpeechBus.rank(name: "Samantha", quality: .enhanced) == 3)
        #expect(SpeechBus.rank(name: "Samantha", quality: .default) == 4)
    }

    @Test @MainActor func showAndRangeHelpers() {
        #expect(parseShowTarget("C3") == ["C3"])
        #expect(parseShowTarget("8").count == 8)
        #expect(wellRangeLabel(["C2", "C3"]) == "C2–C3")
        #expect(fmtClock(90) == "1:30")
        #expect(msLine(["spark": 5]) == "spark 5 ms")
    }

    @Test @MainActor func runBlockRoutesHome() {
        let json = """
        {"version": 5, "plan": {"conditions": {}, "steps": []},
         "step_index": 0, "done": [], "excluded": {}, "redo": {},
         "notes": [], "tips": [], "timers": [], "stamps": {}, "cards": {},
         "run": {"run_id": "run-1", "protocol_id": "abc",
                 "protocol_name": "Demo protocol", "started_at": 1.0}}
        """
        let lab = LabState()
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8)))
        #expect(lab.run?.runId == "run-1")
        #expect(lab.run?.protocolId == "abc")
        // Missing run decodes too: the old fixture has none.
        let bare = LabState()
        bare.sync(Self.snapshot())
        #expect(bare.run == nil)
    }

    @Test @MainActor func stepQuoteFieldsDecode() {
        let json = """
        {"kind": "pipette", "text": "add drug", "quote": "Add 10 uL drug.",
         "quote_page": 2, "wells": ["C2"]}
        """
        let s = try! JSONDecoder().decode(PlanStep.self, from: Data(json.utf8))
        #expect(s.quote == "Add 10 uL drug.")
        #expect(s.quotePage == 2)
    }

    @Test func sourceTargetsParse() {
        let a = parseSourceTarget("protocol:abc#step:3")!
        #expect(a.protocolId == "abc" && a.fragment == "step:3")
        #expect(stepNumber(fragment: a.fragment) == 3)
        let b = parseSourceTarget("protocol:abc#text:30 minutes")!
        #expect(textPhrase(fragment: b.fragment) == "30 minutes")
        #expect(parseSourceTarget("  ") == nil)
        #expect(loosePhrase("10 µL  at  37 °C") == "10 ul at 37 c")
    }

    @Test func sampleBoxPeak() {
        let box = SampleBox()
        let samples: [Float] = [0.1, -0.5, 0.2]
        samples.withUnsafeBufferPointer { box.append($0) }
        #expect(box.samples.count == 3)
        #expect(abs(box.peak - 0.5) < 0.0001)
    }

    @Test func libraryShapesDecode() {
        let p = """
        {"id": "abc", "name": "Demo protocol", "conditions": 3, "steps": 5}
        """
        let ps = try! JSONDecoder().decode(ProtocolSummary.self, from: Data(p.utf8))
        #expect(ps.id == "abc" && ps.steps == 5)
        let r = """
        {"run_id": "run-1", "status": "finished",
         "summary": {"conditions": [], "excluded": 1, "notes": 2, "timers": 0}}
        """
        let rs = try! JSONDecoder().decode(RunSummary.self, from: Data(r.utf8))
        #expect(rs.runId == "run-1" && rs.summary?.notes == 2)
    }
}
