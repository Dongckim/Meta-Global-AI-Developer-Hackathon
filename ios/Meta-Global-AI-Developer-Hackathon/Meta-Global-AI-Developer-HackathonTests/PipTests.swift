import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct PipTests {
    static func snapshot(version: Int = 3) -> RecordSnapshot {
        let json = """
        {"version": \(version),
         "plan": {"groups": {"drug_A": {"wells": ["C2", "C3"], "n": 2}},
                  "steps": [{"name": "s1", "wells": ["B2"]}, {"name": "s2", "wells": ["C2", "C3"]}]},
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
        #expect(lab.groupProgress().first { $0.name == "drug_A" }?.have == 1)
        #expect(lab.groupOf("C2") == "drug_A")
        #expect(lab.currentRange == "C2–C3")
        #expect(lab.hasNote("C2"))
        #expect(lab.memos(for: "C2").map(\.text) == ["bubbles"])
        #expect(lab.wellSource["C2"] == "voice")
    }

    @Test @MainActor func redoWellCountsInGroup() {
        let json = """
        {"version": 4,
         "plan": {"groups": {"drug_A": {"wells": ["C2", "C3", "C9"], "n": 2}}, "steps": []},
         "step_index": 0, "done": ["C9"], "excluded": {}, "redo": {"C9": "backup"},
         "notes": [], "tips": [], "timers": [], "stamps": {}, "cards": {}}
        """
        let lab = LabState()
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8)))
        #expect(lab.groupProgress().first { $0.name == "drug_A" }?.have == 1)
    }

    @Test @MainActor func timerVisibility() {
        let lab = LabState()
        lab.sync(Self.snapshot())
        #expect(lab.visibleTimers(now: Date(timeIntervalSince1970: 1100)).count == 1)
        #expect(lab.visibleTimers(now: Date(timeIntervalSince1970: 100000)).isEmpty)
    }

    @Test @MainActor func showAndRangeHelpers() {
        #expect(parseShowTarget("C3") == ["C3"])
        #expect(parseShowTarget("8").count == 8)
        #expect(wellRangeLabel(["C2", "C3"]) == "C2–C3")
        #expect(fmtClock(90) == "1:30")
        #expect(msLine(["spark": 5]) == "spark 5 ms")
    }
}
