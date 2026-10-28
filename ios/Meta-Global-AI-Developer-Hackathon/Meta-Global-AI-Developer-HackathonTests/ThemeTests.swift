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
        #expect(conn.value == "RB Meta · 85%")
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
