import AVFoundation
import CoreBluetooth
import Foundation
import Speech
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
        #expect(conn.value == "RB Meta · 85%")
        let found = SyncRows.glasses(status: .found, name: "RB Meta",
                                     battery: nil, error: nil)
        #expect(found.state == .failed)
        #expect(found.hint != nil)
        #expect(SyncRows.glasses(status: .registered, name: nil, battery: nil,
                                 error: nil).state == .failed)
        // waits on the Meta AI app row instead of failing on its own
        #expect(SyncRows.glasses(status: .unregistered, name: nil,
                                 battery: nil, error: nil).state == .waiting)
    }

    @Test func audioRow() {
        #expect(SyncRows.glassesAudio(mic: .allowed, glassesConnected: true,
                                      hfpName: "Glasses").state == .ok)
        let off = SyncRows.glassesAudio(mic: .allowed, glassesConnected: true, hfpName: nil)
        #expect(off.state == .failed)
        #expect(off.hint != nil)
    }

    @Test func cameraRow() {
        #expect(SyncRows.camera(.granted).state == .ok)
        // DAT reports never-asked as denied: offer the grant flow
        #expect(SyncRows.camera(.denied).state == .action)
        #expect(SyncRows.camera(nil).state == .failed)
    }
}

struct SyncGateTests {
    private let ok = SyncRows.Row(state: .ok, value: "x", hint: nil)
    private let bad = SyncRows.Row(state: .failed, value: "x", hint: nil)

    @Test func gateNeedsAllPermissionsAndReadyOrSkippedGlasses() {
        let allowed = [AppPermission.State](repeating: .allowed, count: 4)
        #expect(!SyncGate.needed(permissions: allowed, glasses: [ok, ok], skipped: false))
        #expect(SyncGate.needed(permissions: allowed, glasses: [ok, bad], skipped: false))
        // Skip covers the glasses rows only
        #expect(!SyncGate.needed(permissions: allowed, glasses: [ok, bad], skipped: true))
        var missing = allowed
        missing[1] = .notAsked
        #expect(SyncGate.needed(permissions: missing, glasses: [ok, ok], skipped: true))
        missing[1] = .denied
        #expect(SyncGate.needed(permissions: missing, glasses: [ok, ok], skipped: true))
    }

    @Test func rowsBeforeBluetoothWaitOnIt() {
        let rows = SyncGate.rows(permission: { _ in .notAsked }, server: .idle,
                                 dat: nil, hfpName: nil)
        #expect(rows.count == SyncItem.allCases.count)
        for item in [SyncItem.metaApp, .glasses, .camera] {
            #expect(rows[item.rawValue] == SyncRows.needsBluetooth)
        }
        #expect(SyncRows.needsBluetooth.state == .waiting)
        // audio needs no DAT, but asks mic + speech first
        #expect(rows[SyncItem.audio.rawValue].value == "Tap to allow mic")
        for item in [SyncItem.bluetooth, .localNetwork] {
            #expect(rows[item.rawValue].value == "Tap to allow")
            #expect(rows[item.rawValue].state == .action)
        }
    }

    @Test func rowOrderMatchesDesignBrief() {
        #expect(SyncItem.allCases.map(\.title) == [
            "Bluetooth", "Local network", "Meta AI app", "Glasses",
            "Glasses audio", "Camera"])
    }
}

struct PermissionRowTests {
    @Test func permissionStatesToRows() {
        #expect(SyncRows.permission(.notAsked) == .init(state: .action, value: "Tap to allow", hint: nil))
        let denied = SyncRows.permission(.denied)
        #expect(denied.state == .failed && denied.value == "Denied · Open Settings")
        #expect(SyncRows.permission(.allowed) == .init(state: .ok, value: "Allowed", hint: nil))
    }

    @Test func glassesAudioAsksMicThenFollowsGlasses() {
        let ask = SyncRows.glassesAudio(mic: .notAsked, glassesConnected: true, hfpName: "RB")
        #expect(ask.state == .action && ask.value == "Tap to allow mic")
        let off = SyncRows.glassesAudio(mic: .denied, glassesConnected: true, hfpName: "RB")
        #expect(off.state == .failed && !off.value.contains("Denied"))
        #expect(SyncRows.glassesAudio(mic: .allowed, glassesConnected: true, hfpName: "RB").state == .ok)
        #expect(SyncRows.glassesAudio(mic: .allowed, glassesConnected: true, hfpName: nil).state == .failed)
        #expect(SyncRows.glassesAudio(mic: .allowed, glassesConnected: false, hfpName: nil).state == .waiting)
    }

    @Test func localNetworkShowsTheConnection() {
        #expect(SyncRows.localNetwork(allowed: false, link: .idle).value == "Tap to allow")
        #expect(SyncRows.localNetwork(allowed: true, link: .idle).state == .checking)
        #expect(SyncRows.localNetwork(allowed: true, link: .connecting).state == .checking)
        let up = SyncRows.localNetwork(allowed: true, link: .connected)
        #expect(up.state == .ok && up.value == "Connected")
        #expect(SyncRows.localNetwork(allowed: true, link: .failed).state == .failed)
    }

    @Test func tapRowsDoNotTriggerRetrySkip() {
        let tap = SyncRows.permission(.notAsked)
        #expect(!SyncGate.showsActions(rows: [tap, SyncRows.needsBluetooth], pendingFor: nil))
        #expect(Theme.syncStyle(for: .action).rowOpacity == 1)
        #expect(Theme.syncStyle(for: .waiting).rowOpacity == 0.38)
    }
}

struct SyncLayoutTests {
    @Test func sixRowsFitAtDesignSpacing() {
        let fit = SyncLayout.spacing(rows: SyncItem.allCases.count)
        #expect(fit.row == 18 && fit.top == 18)
    }

    @Test func tightHeightShrinksOnlySpacing() {
        let roomy = SyncLayout.spacing(rows: 8, available: 2000)
        #expect(roomy.row == 18)
        let tight = SyncLayout.spacing(rows: 8, available: 600)
        #expect(tight.row < 18 && tight.row > 0 && tight.row == tight.top)
        #expect(SyncLayout.spacing(rows: 8, available: 100).row == 0)
    }
}

struct TalkLineTests {
    @Test func readyLineTellsWhatBlocksTalking() {
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil,
                              wake: .undetermined).text == "Allow mic to talk to Pip")
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil,
                              wake: .micDenied).text == "Allow mic to talk to Pip")
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil,
                              wake: .noOnDevice).text == "Hold the mic to talk to Pip")
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil,
                              wake: .ok, wakeOn: false).text == "Hold the mic to talk to Pip")
        #expect(Theme.pipText(for: .ready, speakText: "", partialText: nil,
                              wake: .ok).text == "Say “Hey Pip”")
        // other states are unaffected
        #expect(Theme.pipText(for: .listening, speakText: "", partialText: nil,
                              wake: .micDenied).text == "Pip Listening")
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
        #expect(SyncRows.camera(.denied, step: .checking).state == .action)
        // audio is checked synchronously: never pending
        #expect(SyncRows.glassesAudio(mic: .allowed, glassesConnected: true,
                                      hfpName: nil).state != .checking)
    }

    @Test func actionsOnFailureOrLongPending() {
        let ok = SyncRows.Row(state: .ok, value: "", hint: nil)
        let pending = SyncRows.Row(state: .checking, value: "", hint: nil)
        let failed = SyncRows.Row(state: .failed, value: "", hint: nil)
        #expect(!SyncGate.showsActions(rows: [ok, pending], pendingFor: 3))
        #expect(SyncGate.showsActions(rows: [ok, pending], pendingFor: 10.5))
        #expect(SyncGate.showsActions(rows: [ok, failed], pendingFor: nil))
        #expect(!SyncGate.showsActions(rows: [ok, ok], pendingFor: nil))
        // pending glasses still need the gate
        #expect(SyncGate.needed(permissions: [.allowed], glasses: [ok, pending], skipped: false))
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

struct PermissionMapTests {
    @Test func osStatusesMapToThreeStates() {
        #expect(AppPermission.map(CBManagerAuthorization.notDetermined) == .notAsked)
        #expect(AppPermission.map(CBManagerAuthorization.allowedAlways) == .allowed)
        #expect(AppPermission.map(CBManagerAuthorization.denied) == .denied)
        #expect(AppPermission.map(CBManagerAuthorization.restricted) == .denied)
        #expect(AppPermission.map(AVAudioApplication.recordPermission.undetermined) == .notAsked)
        #expect(AppPermission.map(AVAudioApplication.recordPermission.granted) == .allowed)
        #expect(AppPermission.map(SFSpeechRecognizerAuthorizationStatus.notDetermined) == .notAsked)
        #expect(AppPermission.map(SFSpeechRecognizerAuthorizationStatus.restricted) == .denied)
        #expect(AppPermission.localNetwork.state(serverAllowed: false) == .notAsked)
        #expect(AppPermission.localNetwork.state(serverAllowed: true) == .allowed)
    }
}
