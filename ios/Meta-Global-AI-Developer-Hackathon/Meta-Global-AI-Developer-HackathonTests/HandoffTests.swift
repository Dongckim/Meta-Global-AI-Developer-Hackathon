import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct HandoffTests {
    /// plate_map.csv fields: quoted commas and doubled quotes stay in one field.
    @Test func csvFields() {
        #expect(HandoffFilesScreen.fields(#"C3,"Drug A",excluded,1,voice,"bubbles, ""big""""#)
                == ["C3", "Drug A", "excluded", "1", "voice", #"bubbles, "big""#])
        #expect(HandoffFilesScreen.fields("B2,,done,1 2,,") == ["B2", "", "done", "1 2", "", ""])
    }

    /// Snapshot `handoff` (server store.handoff_view) decodes; a final row
    /// carries plan → run and its changes for the review views.
    @Test func decodesSnapshotHandoff() throws {
        let json = #"""
        {"epoch": "e", "version": 3, "plan": {"conditions": {}, "steps": []},
         "handoff": {"run_id": "run-1", "label": "Run 1", "approved": true, "stage": "passed", "attempt": 1,
                     "error": "", "check": {"ok": true, "steps": [{"n": 1, "title": "Add PBS", "ok": true, "why": ""}],
                                            "problems": []},
                     "code": "def run(p): pass", "csv": "well,group\n", "exported": null,
                     "meta": {"source": "demo.pdf", "layout": {}, "origin": "pdf", "original_id": "p1",
                              "evidence": {"text": "Add PBS"}},
                     "summary": {"rows": [{"n": 1, "index": 1, "title": "Add PBS", "detail": "10 µL · B2, B4",
                                           "flagged": true, "reason": "B3 skipped", "quote": "Add PBS", "page": 1,
                                           "plan": "10 µL · B2–B4", "run": "10 µL · B2, B4",
                                           "changes": [{"kind": "skipped", "wells": ["B3"], "text": "B3 skipped",
                                                        "event": "skipped", "ts": 5}]}],
                                 "others": [], "conditions": 1, "wells": 3, "steps": 2, "of": 1, "changed": 1}}}
        """#
        let snap = try JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8))
        let h = try #require(snap.handoff)
        #expect(h.runId == "run-1" && h.stage == "passed" && h.check?.steps.first?.ok == true)
        let review = ReviewSource(h)
        #expect(review.mode == .finalProtocol && review.meta.originalId == "p1")
        let row = try #require(review.summary?.rows.first)
        #expect(row.plan == "10 µL · B2–B4" && row.changes?.first?.event == "skipped")
    }

    /// GET /runs/{id} handoff (server store.handoff_code): each step's line range and color.
    @Test func decodesRunCode() throws {
        let json = #"""
        {"run": null, "label": "Run 1",
         "handoff": {"code": "a\nb\nc", "setup": [1, 1], "exported": true,
                     "steps": [{"n": 1, "title": "Add medium", "detail": "100 µL · B2", "plan": "100 µL · B2",
                                "changes": [], "lines": [2, 3], "color": "#F7C6D0"}]}}
        """#
        struct Only: Decodable { let handoff: RunCode? }
        let code = try #require(try JSONDecoder().decode(Only.self, from: Data(json.utf8)).handoff)
        #expect(code.steps.first?.lines == [2, 3] && code.steps.first?.color == "#F7C6D0" && code.exported)
    }
}
