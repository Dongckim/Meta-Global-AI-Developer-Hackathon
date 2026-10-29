import Foundation
import MWDATCore
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct OrbStyleTests {
    @Test func readyBreathesWithRing() {
        let s = Theme.orbStyle(for: .ready)
        #expect(s.breathe == Theme.breathe)
        #expect(s.ring == Theme.orbRingPeriod)
        #expect(s.scale == 1)
        #expect(s.aurora == .breath)
        #expect(s.hollow == false)
    }

    @Test func listeningIsFastAndLarge() {
        let s = Theme.orbStyle(for: .listening)
        #expect(s.breathe == Theme.breatheListening)
        #expect(s.ring == Theme.orbRingListening)
        #expect(s.scale == 1.32)
        #expect(s.aurora == .pulse)
        #expect(s.hollow == false)
    }

    @Test func transcribingHoldsStill() {
        let s = Theme.orbStyle(for: .transcribing)
        #expect(s.breathe == nil)
        #expect(s.ring == nil)
        #expect(s.scale == 1)
        #expect(s.aurora == .still(scale: 1.15))
        #expect(s.hollow == false)
    }

    @Test func thinkingIsSlowNoRing() {
        let s = Theme.orbStyle(for: .thinking)
        #expect(s.breathe == Theme.breatheThinking)
        #expect(s.ring == nil)
        #expect(s.scale == 1.12)
        #expect(s.aurora == .think)
        #expect(s.hollow == false)
    }

    @Test func speakingIsHollowVoice() {
        let s = Theme.orbStyle(for: .speaking)
        #expect(s.breathe == nil)
        #expect(s.ring == nil)
        #expect(s.hollow == true)
        #expect(s.aurora == .voice(amp: 0))
    }

    @Test func searchingSweeps() {
        let s = Theme.orbStyle(for: .searching)
        #expect(s.aurora == .sweep)
        #expect(s.hollow == false)
    }
}

struct PipLineTests {
    @Test func copyPerState() {
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil)
            == Theme.PipLine(text: "Say “Hey Pip”", dots: false))
        #expect(Theme.pipText(for: .listening, speakText: "", partialText: nil)
            == Theme.PipLine(text: "Pip Listening", dots: false))
        #expect(Theme.pipText(for: .thinking, speakText: "", partialText: nil)
            == Theme.PipLine(text: "Pip Thinking", dots: true))
        #expect(Theme.pipText(for: .searching, speakText: "", partialText: nil)
            == Theme.PipLine(text: "Pip Searching", dots: true))
    }

    @Test func transcribingShowsPartialSlot() {
        #expect(Theme.pipText(for: .transcribing, speakText: "",
                              partialText: nil).dots == true)
        let live = Theme.pipText(for: .transcribing, speakText: "",
                                 partialText: "Note B4, small bubble")
        #expect(live == Theme.PipLine(text: "Note B4, small bubble",
                                       dots: false))
    }

    @Test func speakingShowsReplyVerbatim() {
        let live = Theme.pipText(for: .speaking, speakText: "C3 excluded.",
                                 partialText: nil)
        #expect(live == Theme.PipLine(text: "C3 excluded.", dots: false))
        #expect(Theme.pipText(for: .speaking, speakText: "",
                              partialText: nil).dots == true)
    }
}

struct SyncStyleTests {
    @Test func iconPerRowState() {
        #expect(Theme.syncStyle(for: .checking).icon == .spin)
        #expect(Theme.syncStyle(for: .ok).icon == .tick)
        #expect(Theme.syncStyle(for: .failed).icon == .hollow)
        #expect(Theme.syncStyle(for: .ok).rowOpacity == 1)
        #expect(Theme.syncStyle(for: .checking).rowOpacity == 1)
        #expect(Theme.syncStyle(for: .failed).rowOpacity == 0.38)
    }
}

struct SyncRowTests {
    @Test func appRow() {
        #expect(SyncRows.app(registration: .registered, error: nil).state == .ok)
        #expect(SyncRows.app(registration: .registering, error: nil).state
            == .checking)
        let missing = SyncRows.app(registration: .available, error: nil)
        #expect(missing.state == .failed)
        #expect(missing.hint != nil)
        #expect(SyncRows.app(registration: .unavailable, error: nil).state
            == .failed)
        let err = SyncRows.app(registration: .available, error: "denied")
        #expect(err.state == .failed)
        #expect(err.hint == "denied")
    }

    @Test func glassesRow() {
        let conn = SyncRows.glasses(status: .connected, name: "RB Meta",
                                    battery: 85, error: nil)
        #expect(conn.state == .ok)
        #expect(conn.value == "Found · 85%")
        let found = SyncRows.glasses(status: .found, name: "RB Meta",
                                     battery: nil, error: nil)
        #expect(found.state == .failed)
        #expect(found.hint != nil)
        #expect(SyncRows.glasses(status: .registered, name: nil, battery: nil,
                                 error: nil).state == .failed)
        #expect(SyncRows.glasses(status: .unregistered, name: nil,
                                 battery: nil, error: nil).state == .failed)
    }

    @Test func audioRow() {
        #expect(SyncRows.audio(hfpName: "Glasses").state == .ok)
        let off = SyncRows.audio(hfpName: nil)
        #expect(off.state == .failed)
        #expect(off.hint != nil)
    }

    @Test func cameraRow() {
        #expect(SyncRows.camera(.granted).state == .ok)
        #expect(SyncRows.camera(.denied).state == .failed)
        #expect(SyncRows.camera(nil).state == .failed)
    }
}

struct SyncGateTests {
    private func rows(allOk: Bool) -> [SyncRows.Row] {
        let bad = SyncRows.Row(state: .failed, value: "x", hint: nil)
        let good = SyncRows.Row(state: .ok, value: "x", hint: nil)
        return allOk ? [good, good, good, good] : [good, bad, good, good]
    }

    @Test func gateShowsUntilConnectedOrSkipped() {
        #expect(SyncGate.needed(rows: rows(allOk: false), skipped: false)
            == true)
        #expect(SyncGate.needed(rows: rows(allOk: true), skipped: false)
            == false)
        #expect(SyncGate.needed(rows: rows(allOk: false), skipped: true)
            == false)
        #expect(SyncGate.needed(rows: rows(allOk: true), skipped: true)
            == false)
    }
}

struct KeyframesTests {
    @Test func bezierEndpointsAndCurves() {
        #expect(Keyframes.ease(0) == 0)
        #expect(abs(Keyframes.ease(1) - 1) < 1e-6)
        // --ease front-loads: well past halfway at 30%
        #expect(Keyframes.ease(0.3) > 0.6)
        #expect(abs(Keyframes.easeInOut(0.5) - 0.5) < 1e-4)
        // --spring overshoots before settling
        #expect((0 ..< 100).contains { Keyframes.spring(Double($0) / 100) > 1 })
    }

    @Test func pingPongPeaksAtHalfPeriod() {
        #expect(Keyframes.pingPong(0, period: 6) == 0)
        #expect(abs(Keyframes.pingPong(3, period: 6) - 1) < 1e-6)
        #expect(abs(Keyframes.pingPong(6, period: 6)) < 1e-6)
        #expect(abs(Keyframes.pingPong(1, period: 6) - Keyframes.pingPong(5, period: 6)) < 1e-9)
    }
}

struct SyncPendingTests {
    @Test func pendingStepsPerRow() {
        let link = SyncRows.app(registration: .registering, error: nil)
        #expect(link.state == .checking && link.value == "Linking…")
        let search = SyncRows.glasses(status: .registered, name: nil, battery: nil,
                                      error: nil, searching: true)
        #expect(search.state == .checking && search.value == "Searching nearby…")
        // after the first devices event, no devices is a failure
        #expect(SyncRows.glasses(status: .registered, name: nil, battery: nil,
                                 error: nil, searching: false).state == .failed)
        let ask = SyncRows.camera(.denied, step: .asking)
        #expect(ask.state == .checking && ask.value == "Asking…")
        #expect(SyncRows.camera(nil, step: .checking).state == .checking)
        // a re-check keeps a known answer instead of flickering to pending
        #expect(SyncRows.camera(.granted, step: .checking).state == .ok)
        #expect(SyncRows.camera(.denied, step: .checking).state == .failed)
        // audio is checked synchronously: never pending
        #expect(SyncRows.audio(hfpName: nil).state == .failed)
    }

    @Test func actionsOnFailureOrLongPending() {
        let ok = SyncRows.Row(state: .ok, value: "", hint: nil)
        let pending = SyncRows.Row(state: .checking, value: "", hint: nil)
        let failed = SyncRows.Row(state: .failed, value: "", hint: nil)
        #expect(!SyncGate.showsActions(rows: [ok, pending], pendingFor: 3))
        #expect(SyncGate.showsActions(rows: [ok, pending], pendingFor: 10.5))
        #expect(SyncGate.showsActions(rows: [ok, failed], pendingFor: nil))
        #expect(!SyncGate.showsActions(rows: [ok, ok], pendingFor: nil))
        // pending does not change the gate decision
        #expect(SyncGate.needed(rows: [ok, pending], skipped: false))
    }
}

struct FrameClockTests {
    @Test func stallPausesInsteadOfSkipping() {
        let c = FrameClock()
        let t0 = Date(timeIntervalSince1970: 1000)
        #expect(c.tick(t0) == 0) // starts at the first frame
        #expect(abs(c.tick(t0.addingTimeInterval(1.0 / 60)) - 1.0 / 60) < 1e-6)
        // a 300ms stall advances one capped frame, not 300ms
        let after = c.tick(t0.addingTimeInterval(1.0 / 60 + 0.3))
        #expect(abs(after - (1.0 / 60 + 1.0 / 30)) < 1e-6)
        // same date again adds nothing
        #expect(c.tick(t0.addingTimeInterval(1.0 / 60 + 0.3)) == after)
    }
}
