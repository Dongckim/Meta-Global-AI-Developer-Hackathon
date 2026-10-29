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
                             "tip": null, "caution": null, "pipette": null, "wells": ["C2", "C3"]}]},
         "step_index": 1, "done": [{"step": 2, "well": "C2", "source": "voice", "ts": 1.0}], "excluded": {}, "redo": {},
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
        #expect(lab.cards.map(\.question) == ["Redo C4?", "Exclude C3?"])
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

    @Test @MainActor func doneSplitsByStep() {
        let json = """
        {"version": 1,
         "plan": {"conditions": {"a": {"label": "A", "treatments": [], "replicates": 2,
                                       "wells": ["B2", "C2"]}},
                  "steps": [{"kind": "add", "text": "media", "wells": ["B2", "C2"]},
                            {"kind": "add", "text": "drug", "wells": ["C2"]}]},
         "step_index": 1,
         "done": [{"step": 1, "well": "B2", "source": "voice", "ts": 1.0},
                  {"step": 1, "well": "C2", "source": "voice", "ts": 1.0},
                  {"step": 2, "well": "C2", "source": "tap", "ts": 2.0}],
         "cards": {}}
        """
        let lab = LabState()
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(json.utf8)))
        #expect(lab.done == ["C2"]) // filled: done this step
        #expect(lab.doneEarlier == ["B2"]) // dimmed: done only before
        #expect(lab.conditionProgress().first?.have == 2) // any step counts
        let legacy = Data(#"{"version": 1, "plan": {"conditions": {}, "steps": []}, "done": ["C2"]}"#.utf8)
        #expect(throws: DecodingError.self) { // API 4 bare wells are a contract break
            _ = try JSONDecoder().decode(RecordSnapshot.self, from: legacy)
        }
    }

    @Test @MainActor func redoWellCountsInGroup() {
        let json = """
        {"version": 4,
         "plan": {"conditions": {"drug_A": {"label": "Drug A", "treatments": ["Drug A"],
                                            "replicates": 2, "wells": ["C2", "C3", "C9"]}}, "steps": []},
         "step_index": 0, "done": [{"step": 1, "well": "C9", "source": "voice", "ts": 1.0}], "excluded": {}, "redo": {"C9": "backup"},
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

    @Test func voiceChoiceFollowsPersona() {
        let p = Persona(agentName: "Milo", wakePhrase: "Hey Milo", voice: "Jamie", quality: "premium",
                        language: "en-GB", rate: 0.52, pitch: 0.92)
        let jp = VoiceCandidate(id: "j1", name: "Jamie", language: "en-GB", quality: .premium)
        let je = VoiceCandidate(id: "j2", name: "Jamie", language: "en-GB", quality: .enhanced)
        let zp = VoiceCandidate(id: "z", name: "Zoe", language: "en-GB", quality: .premium)
        let ap = VoiceCandidate(id: "a", name: "Ava", language: "en-US", quality: .premium)
        #expect(SpeechBus.choose([ap, zp, je, jp], persona: p)?.id == "j1") // name + quality
        #expect(SpeechBus.choose([ap, zp, je], persona: p)?.id == "j2")     // same name enhanced
        #expect(SpeechBus.choose([ap, zp], persona: p)?.id == "z")          // same-language premium
        #expect(SpeechBus.choose([ap], persona: p) == nil)                  // system default
    }

    @Test func screenRegistryLoadsFromBundleWithoutFallback() {
        let r = loadScreens()
        #expect(r.error == nil)
        let ids = Set(r.screens.map(\.id))
        // the shared registry, not a built-in list (importer exists only there)
        #expect(ids.isSuperset(of: ["main", "home", "protocol_source", "importer", "sync"]))
        let missing = loadScreens(from: nil)
        #expect(missing.screens.isEmpty && missing.error != nil)
    }

    @Test func screenRegistryReadsOnlyScreens() throws {
        // reads/use/not belong to the server and must not break the app's decode
        let ok = Data(#"{"screens": [{"id": "home", "commands": ["back"], "use": "x"}], "reads": [{"kind": "runs", "screens": [], "desc": "d"}]}"#.utf8)
        #expect(try ScreenRegistry.decode(ok).map(\.id) == ["home"])
        #expect(throws: (any Error).self) { try ScreenRegistry.decode(Data(#"{"screens": []}"#.utf8)) }
        #expect(throws: (any Error).self) { try ScreenRegistry.decode(Data(#"{"screens": [{"id": 1}]}"#.utf8)) }
    }

    @Test func personaLoadsFromBundledConfig() {
        let p = Persona.current
        #expect(p != Persona.fallback) // pip_voice.json is in the app bundle
        #expect(!p.agentName.isEmpty && !p.wakePhrase.isEmpty && !p.voice.isEmpty)
        #expect(Persona.load(from: nil) == Persona.fallback)
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

    @Test func syncReasonNamesBreaks() {
        let bad = Data(#"{"plan": {"conditions": {}, "steps": []}}"#.utf8)
        do {
            _ = try JSONDecoder().decode(RecordSnapshot.self, from: bad)
            Issue.record("missing version should throw")
        } catch {
            #expect(syncReason(error) == "version missing")
        }
        let bad2 = Data(#"{"version": 1, "plan": {"conditions": {}, "steps": []}, "done": "C2"}"#.utf8)
        do {
            _ = try JSONDecoder().decode(RecordSnapshot.self, from: bad2)
            Issue.record("wrong-typed done should throw")
        } catch {
            #expect(syncReason(error) == "done wrong type")
        }
        let ok = try! JSONDecoder().decode(
            RecordSnapshot.self,
            from: Data(#"{"version": 3, "plan": {"conditions": {}, "steps": []}}"#.utf8))
        #expect(ok.version == 3 && ok.done.isEmpty && ok.stepIndex == 0)
        let rc = try! JSONDecoder().decode(
            PlanCondition.self,
            from: Data(#"{"label": "X", "treatments": [], "replicates": null, "wells": ["A1", "A2"]}"#.utf8))
        #expect(rc.replicates == 2)
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

    @Test @MainActor func emptyRecordAfterEndSyncs() {
        let live = """
        {"version": 7, "run": {"run_id": "run-1", "protocol_id": "p",
         "protocol_name": "Demo", "started_at": 1.0},
         "plan": {"conditions": {}, "steps": []}, "step_index": 1,
         "done": [{"step": 2, "well": "C2", "source": "voice", "ts": 1.0}], "cards": {"c1": {"id": "c1", "kind": "end_run",
          "question": "End this run?", "detail": null, "ts": 2.0}}}
        """
        let lab = LabState()
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(live.utf8)))
        #expect(lab.run?.runId == "run-1" && lab.cards.count == 1)
        let empty = """
        {"version": 8, "run": null,
         "plan": {"conditions": {}, "steps": [], "backups": []},
         "step_index": 0, "done": [], "excluded": {}, "redo": {},
         "notes": [], "tips": [], "timers": [], "stamps": {},
         "recent": [], "cards": {}}
        """
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(empty.utf8)))
        #expect(lab.run == nil)
        #expect(lab.cards.isEmpty)
        #expect(lab.done.isEmpty && lab.version == 8)
    }

    @Test @MainActor func viewDecodes() {
        let lab = LabState()
        let snap = """
        {"epoch": "a", "version": 1, "run": null,
         "view": {"current": {"screen": "run_view", "target": "run-1"},
                  "stack": [{"screen": "main", "target": ""}],
                  "tab": "steps",
                  "sheet": {"name": "well_card", "target": "C5"}},
         "plan": {"conditions": {}, "steps": []},
         "done": [], "cards": {}}
        """
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(snap.utf8)))
        #expect(lab.view.current == ViewTarget(screen: "run_view", target: "run-1"))
        #expect(lab.view.stack == [ViewTarget(screen: "main", target: "")])
        #expect(lab.view.tab == "steps")
        #expect(lab.view.sheet == ViewSheet(name: "well_card", target: "C5"))
        #expect(Self.snapshot().view == ViewState())
    }

    @Test @MainActor func epochChangeResetsStaleGuard() {
        let lab = LabState()
        let live = """
        {"epoch": "aaa", "version": 7, "run": {"run_id": "run-1",
         "protocol_id": "p", "protocol_name": "Demo", "started_at": 1.0},
         "plan": {"conditions": {}, "steps": []},
         "done": [{"step": 1, "well": "C2", "source": "voice", "ts": 1.0}], "cards": {}}
        """
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(live.utf8)))
        #expect(lab.version == 7 && lab.epoch == "aaa")
        // Data reset: lower version, new epoch -> accept anyway.
        let reset = """
        {"epoch": "bbb", "version": 2, "run": null,
         "plan": {"conditions": {}, "steps": []},
         "done": [], "cards": {}}
        """
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(reset.utf8)))
        #expect(lab.version == 2 && lab.epoch == "bbb")
        #expect(lab.run == nil && lab.done.isEmpty)
        // Same epoch: older version still ignored.
        let stale = """
        {"epoch": "bbb", "version": 1, "run": null,
         "plan": {"conditions": {}, "steps": []},
         "done": [{"step": 1, "well": "C9", "source": "voice", "ts": 1.0}], "cards": {}}
        """
        lab.sync(try! JSONDecoder().decode(RecordSnapshot.self, from: Data(stale.utf8)))
        #expect(lab.version == 2 && lab.done.isEmpty)
    }

    @Test func wavBytesDuration() {
        func u32(_ v: Int) -> Data { withUnsafeBytes(of: UInt32(v).littleEndian) { Data($0) } }
        func chunk(_ id: String, _ body: Data) -> Data { Data(id.utf8) + u32(body.count) + body }
        /// 16kHz mono 16-bit; `filler` adds a padding chunk before data like AVAudioFile.
        func wav(frames: Int, filler: Int = 0) -> Data {
            var fmt = Data([1, 0, 1, 0]) + u32(16000) + u32(32000) // PCM, mono, rate, byteRate
            fmt += Data([2, 0, 16, 0])
            var body = Data("WAVE".utf8) + chunk("fmt ", fmt)
            if filler > 0 { body += chunk("FLLR", Data(count: filler)) }
            body += chunk("data", Data(count: frames * 2))
            return Data("RIFF".utf8) + u32(body.count) + body
        }
        #expect(Recorder.duration(of: wav(frames: 0)) == 0)
        #expect(abs((Recorder.duration(of: wav(frames: 1600)) ?? -1) - 0.1) < 0.001)
        #expect(abs((Recorder.duration(of: wav(frames: 16000)) ?? -1) - 1.0) < 0.001)
        #expect(Recorder.duration(of: wav(frames: 0, filler: 4000)) == 0)
        #expect(abs((Recorder.duration(of: wav(frames: 1600, filler: 4000)) ?? -1) - 0.1) < 0.001)
        #expect(Recorder.duration(of: Data(count: 10)) == nil)
        #expect(Recorder.duration(of: Data(count: 4096)) == nil)
    }
}
