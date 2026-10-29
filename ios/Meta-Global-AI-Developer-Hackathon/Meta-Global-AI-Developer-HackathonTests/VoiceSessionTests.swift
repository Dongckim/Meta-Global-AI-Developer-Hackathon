import AVFoundation
import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct WakeMatcherTests {
    @Test func positives() {
        for t in ["hey pip", "Hey Pip", "HEY PIP!", "hey, pip", "say hey pip now",
                  "hey pep", "hey peep", "um hey pip please", "hay pip"] {
            #expect(WakeMatcher.matches(t), Comment(rawValue: t))
        }
    }

    @Test func negatives() {
        for t in ["", "hey", "pip", "happy", "pipette the sample", "pass the pipe",
                  "hey pipe it", "hey pick C2", "keep it", "hey pit crew",
                  "hey papa", "this is a test", "peptalk"] {
            #expect(!WakeMatcher.matches(t), Comment(rawValue: t))
        }
    }
}

@MainActor
final class FakeWake: WakeControlling {
    var onWake: (() -> Bool)?
    var onFailed: (() -> Void)?
    var running = false
    var debugDetail = "fake"
    var starts = 0
    var stops = 0
    func start() async throws { starts += 1; running = true }
    func stop() { stops += 1; running = false }
    @discardableResult func fire() -> Bool { onWake?() ?? false }
}

@MainActor
final class FakeCapture: CaptureControlling {
    /// Scripted levels, one per read; falls back to basePeak when drained.
    /// Deterministic under any poll timing (no wall-clock races).
    var script: [Float] = []
    var basePeak: Float = 0
    var peak: Float {
        get { script.isEmpty ? basePeak : script.removeFirst() }
    }
    var startDelay: UInt64 = 0
    var starts = 0
    var stops = 0
    var failStart = false
    func start() async throws {
        starts += 1
        if startDelay > 0 { try? await Task.sleep(nanoseconds: startDelay) }
        if failStart { throw RecorderError.noInput }
    }
    func stop() throws -> URL {
        stops += 1
        return URL(fileURLWithPath: "/tmp/fake.wav")
    }
}

@MainActor
final class FakeSpeaker: SpeakerControlling {
    var texts: [String?] = []
    var stops = 0
    private var done: (() -> Void)?
    func speak(_ text: String?, onDone: @escaping () -> Void) {
        texts.append(text)
        done = onDone
    }
    func stop() { stops += 1 }
    func complete() { let d = done; done = nil; d?() }
}

@MainActor
final class Flag {
    var value = false
}

@MainActor
struct VoiceSessionTests {
    private func makeSession(wake: FakeWake? = nil,
                             capture: FakeCapture? = nil,
                             speaker: FakeSpeaker? = nil,
                             chimed: Flag? = nil,
                             mic: (() async -> Bool)? = nil,
                             stuck: TimeInterval = 45,
                             speakMargin: TimeInterval = 5) -> VoiceSession {
        VoiceSession.sharedWakeOn()
        let flag = chimed ?? Flag()
        return VoiceSession(wake: wake ?? FakeWake(), capture: capture ?? FakeCapture(),
                            speaker: speaker ?? FakeSpeaker(),
                            chime: { flag.value = true },
                            supportStub: { .ok },
                            micRequest: mic,
                            silenceTimeout: 0.05, minCapture: 0.02,
                            maxCapture: 5, awaitSpeech: 0.1, stuckTimeout: stuck,
                            speakMargin: speakMargin)
    }

    private func settle(until: @escaping () -> Bool, timeout: Double = 3) async -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if until() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return until()
    }

    @Test func wakeOpensWithChimeAndAutoStops() async {
        let chimed = Flag()
        let wake = FakeWake(), capture = FakeCapture(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, capture: capture, speaker: speaker, chimed: chimed)
        voice.activate()
        capture.script = [0.5, 0.5, 0, 0, 0, 0] // speech, then silence -> auto stop
        voice.open(trigger: .wakeWord)
        #expect(chimed.value)
        #expect(await settle { voice.state == .listening })
        #expect(wake.stops == 1)
        #expect(await settle { voice.state == .transcribing })
        #expect(voice.pendingAudio != nil)
        #expect(capture.stops == 1)
        _ = speaker
    }

    @Test func manualRoundTripSilent() async {
        let chimed = Flag()
        let voice = makeSession(chimed: chimed)
        voice.activate()
        voice.open(trigger: .manual)
        #expect(!chimed.value)
        #expect(await settle { voice.state == .listening })
        voice.endManual()
        #expect(await settle { voice.state == .transcribing })
        #expect(voice.pendingAudio != nil)
    }

    @Test func releaseBeforePermissionAbortsCleanly() async {
        let capture = FakeCapture()
        let voice = makeSession(capture: capture, mic: { true })
        voice.activate()
        voice.beginManual()
        voice.endManual() // released before permission/task resumed
        try? await Task.sleep(for: .milliseconds(500))
        #expect(capture.starts == 0) // engine never spun up
        #expect(voice.state == .ready)
        #expect(voice.pendingAudio == nil)
    }

    @Test func micDeniedEmitsNotice() async {
        let capture = FakeCapture()
        let voice = makeSession(capture: capture, mic: { false })
        voice.activate()
        voice.beginManual()
        #expect(await settle { voice.noticeGen > 0 })
        #expect(voice.notice == "Microphone permission needed")
        #expect(capture.starts == 0)
        #expect(voice.state == .ready)
    }

    @Test func restartGateTripsOnRapidFailures() {
        let t0 = Date()
        var g = RestartGate(limit: 5, window: 10)
        for i in 0..<5 {
            let trip = g.note(date: t0.addingTimeInterval(Double(i)))
            #expect(!trip)
        }
        let trip = g.note(date: t0.addingTimeInterval(5))
        #expect(trip)
        let later = g.note(date: t0.addingTimeInterval(100)) // old marks expired
        #expect(!later)
    }

    @Test func quickTapCancelsPendingStart() async {
        let chimed = Flag()
        let capture = FakeCapture()
        capture.startDelay = 200_000_000
        let voice = makeSession(capture: capture, chimed: chimed)
        voice.activate()
        voice.open(trigger: .manual)
        #expect(await settle { capture.starts == 1 })
        voice.endManual() // released mid-spin-up
        #expect(await settle { capture.stops == 1 })
        #expect(voice.state == .ready)
        #expect(voice.pendingAudio == nil)
    }

    @Test func responseRoutesThinkingSearching() async {
        let chimed = Flag()
        let voice = makeSession(chimed: chimed)
        voice.didRespond(hasSources: false)
        #expect(voice.state == .thinking)
        voice.didRespond(hasSources: true)
        #expect(voice.state == .searching)
    }

    @Test func speakCompletesToReadyAndResumesWake() async {
        let chimed = Flag()
        let wake = FakeWake(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, speaker: speaker, chimed: chimed)
        voice.activate()
        #expect(await settle { wake.starts >= 1 }) // resume on activate
        voice.didRespond(hasSources: false)
        voice.speak("hi")
        #expect(voice.state == .speaking)
        #expect(speaker.texts == ["hi"])
        speaker.complete()
        #expect(await settle { voice.state == .ready })
        #expect(await settle { wake.starts >= 2 }) // resumed after speech
    }

    @Test func speakRefusedWhileListening() async {
        let chimed = Flag()
        let speaker = FakeSpeaker()
        let voice = makeSession(speaker: speaker, chimed: chimed)
        voice.open(trigger: .manual)
        #expect(await settle { voice.state == .listening })
        voice.speak("nope")
        #expect(voice.state == .listening)
        #expect(speaker.texts.isEmpty)
        voice.endManual()
    }

    @Test func wakeIgnoredWhileSpeaking() async {
        let chimed = Flag()
        let capture = FakeCapture(), speaker = FakeSpeaker()
        let voice = makeSession(capture: capture, speaker: speaker, chimed: chimed)
        voice.activate()
        voice.didRespond(hasSources: false)
        voice.speak("talking")
        #expect(voice.state == .speaking)
        voice.open(trigger: .wakeWord)
        #expect(!chimed.value)
        #expect(voice.state == .speaking)
        #expect(capture.starts == 0)
        speaker.complete()
    }

    @Test func manualBargesInOverSpeech() async {
        let chimed = Flag()
        let speaker = FakeSpeaker()
        let voice = makeSession(speaker: speaker, chimed: chimed)
        voice.activate()
        voice.didRespond(hasSources: false)
        voice.speak("talking")
        voice.open(trigger: .manual)
        #expect(speaker.stops == 1)
        #expect(await settle { voice.state == .listening })
        voice.endManual()
    }

    @Test func didFailResets() async {
        let chimed = Flag()
        let voice = makeSession(chimed: chimed)
        voice.open(trigger: .manual)
        #expect(await settle { voice.state == .listening })
        voice.endManual()
        #expect(await settle { voice.state == .transcribing })
        voice.didFail()
        #expect(voice.state == .ready)
        #expect(voice.pendingAudio == nil)
    }

    @Test func noSpeechGivesUpQuietly() async {
        let chimed = Flag()
        let capture = FakeCapture() // peak stays 0
        let voice = makeSession(capture: capture, chimed: chimed)
        let gen = voice.noticeGen
        voice.activate()
        voice.open(trigger: .wakeWord)
        #expect(await settle(timeout: 4) { voice.state == .ready && capture.stops >= 1 })
        #expect(voice.pendingAudio == nil)
        #expect(voice.noticeGen == gen) // silent: a stray detection, not a turn
    }

    @Test func wakeKeepsEngineUntilCaptureAttached() async {
        let wake = FakeWake(), capture = FakeCapture()
        capture.startDelay = 200_000_000
        let voice = makeSession(wake: wake, capture: capture)
        voice.activate()
        #expect(await settle { wake.running })
        voice.open(trigger: .manual)
        #expect(await settle { capture.starts == 1 })
        #expect(wake.running) // still attached while capture spins up
        #expect(await settle { voice.state == .listening })
        #expect(!wake.running)
        voice.endManual()
    }

    @Test func wakeIgnoredWhileCaptureStarting() async {
        let chimed = Flag()
        let wake = FakeWake(), capture = FakeCapture()
        capture.startDelay = 200_000_000
        let voice = makeSession(wake: wake, capture: capture, chimed: chimed)
        voice.activate()
        voice.open(trigger: .manual)
        wake.fire()
        #expect(!chimed.value)
        #expect(await settle { voice.state == .listening })
        #expect(capture.starts == 1)
        voice.endManual()
    }

    @Test func audioLostRestartsWakeAndEndsCapture() async {
        let wake = FakeWake(), capture = FakeCapture()
        let voice = makeSession(wake: wake, capture: capture)
        voice.activate()
        #expect(await settle { wake.starts == 1 })
        voice.audioLost() // idle: wake restarts from scratch
        #expect(wake.stops >= 1)
        #expect(await settle { wake.starts == 2 })
        voice.open(trigger: .manual)
        #expect(await settle { voice.state == .listening })
        voice.audioLost() // mid-capture: finishes like a release
        #expect(voice.state == .transcribing)
        #expect(capture.stops == 1)
    }

    /// Ready baseline every cycle must return to.
    private func expectIdle(_ voice: VoiceSession, _ wake: FakeWake,
                            _ capture: FakeCapture, cycles: Int) async {
        #expect(voice.state == .ready)
        #expect(voice.activeTrigger == nil)
        #expect(voice.pendingAudio == nil)
        #expect(voice.listeningSince == nil)
        #expect(capture.starts == cycles && capture.stops == cycles)
        #expect(await settle { wake.running }) // wake resumed
    }

    @Test func threeManualCyclesReturnToIdle() async {
        let wake = FakeWake(), capture = FakeCapture(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, capture: capture, speaker: speaker, mic: { true })
        voice.activate()
        for i in 1...3 {
            voice.beginManual()
            #expect(await settle { voice.state == .listening })
            voice.endManual()
            #expect(voice.state == .transcribing)
            voice.didRespond(hasSources: false)
            voice.speak("Marked C\(i) done.")
            #expect(voice.state == .speaking)
            speaker.complete()
            await expectIdle(voice, wake, capture, cycles: i)
        }
    }

    @Test func threeWakeCyclesReturnToIdle() async {
        let wake = FakeWake(), capture = FakeCapture(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, capture: capture, speaker: speaker)
        voice.activate()
        for i in 1...3 {
            #expect(await settle { wake.running })
            capture.script = [0.5, 0.5, 0, 0, 0, 0]
            wake.fire()
            #expect(await settle { voice.state == .transcribing })
            voice.didRespond(hasSources: false)
            voice.speak("Okay.")
            speaker.complete()
            await expectIdle(voice, wake, capture, cycles: i)
        }
    }

    @Test func speakingTimesOutWithoutTTSDone() async {
        let wake = FakeWake(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, speaker: speaker, speakMargin: 0.1)
        voice.activate()
        voice.speak("") // speaker never calls onDone
        #expect(voice.state == .speaking)
        #expect(await settle { voice.state == .ready })
        #expect(speaker.stops >= 1)
        #expect(await settle { wake.running })
    }

    @Test func admissionTable() {
        typealias V = VoiceSession
        let all: [V.State] = [.ready, .listening, .transcribing, .thinking, .speaking, .searching]
        for s in all {
            for starting in [false, true] {
                let wake = V.admission(state: s, trigger: .wakeWord, starting: starting)
                let manual = V.admission(state: s, trigger: .manual, starting: starting)
                let label = Comment(rawValue: "\(s) starting=\(starting)")
                switch s {
                case .ready:
                    #expect(wake == (starting ? .ignore : .accept), label)
                    #expect(manual == (starting ? .ignore : .accept), label)
                case .speaking:
                    #expect(wake == .ignore, label)
                    #expect(manual == .bargeIn, label)
                default:
                    #expect(wake == .ignore, label)
                    #expect(manual == .ignore, label)
                }
            }
        }
    }

    @Test func wakeAcceptedFromReadyAfterEveryCycle() async {
        let wake = FakeWake(), capture = FakeCapture(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, capture: capture, speaker: speaker)
        voice.activate()
        for _ in 1...3 {
            #expect(await settle { wake.running })
            capture.script = [0.5, 0.5, 0, 0, 0, 0]
            #expect(wake.fire()) // ready must accept (was "ignored: wake while ready")
            #expect(await settle { voice.state == .transcribing })
            #expect(voice.activeTrigger == nil) // wake capture released its trigger
            voice.didRespond(hasSources: false)
            voice.speak("Okay.")
            speaker.complete()
        }
    }

    @Test func rejectedWakeReportsFalse() async {
        let wake = FakeWake(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, speaker: speaker)
        voice.activate()
        voice.speak("talking")
        #expect(!wake.fire()) // speaking ignores wake: listener must not consume it
        speaker.complete()
        #expect(await settle { voice.state == .ready })
        #expect(await settle { wake.running })
        #expect(wake.fire())
    }

    @Test func wakeLatchConsumesOnlyAccepted() {
        var latch = WakeLatch()
        #expect(latch.hear("hey pip"))
        // rejected: fresh task, nothing carried -> the next "Hey Pip" offers again
        latch.newTask(carry: nil)
        #expect(!latch.hear("show c5")) // stale phrase gone
        #expect(latch.hear("hey pip"))
        latch.accept()
        #expect(!latch.hear("hey pip hey pip")) // accepted: held until a new task
        latch.newTask(carry: "um hey")
        #expect(latch.hear("pip")) // split across the boundary still matches
    }

    @Test func stuckStateReturnsToReady() async {
        let voice = makeSession(stuck: 0.1)
        voice.activate()
        voice.didRespond(hasSources: false) // reply that never speaks
        #expect(voice.state == .thinking)
        #expect(await settle { voice.state == .ready })
    }

    @Test func silenceGate() {
        let t0 = Date()
        var g = SilenceGate(threshold: 0.03, silenceTimeout: 1, minDuration: 0.5, start: t0)
        let quiet = g.feed(rms: 0, now: t0.addingTimeInterval(5)) // never any speech
        #expect(!quiet)
        #expect(!g.heardSpeech)
        let started = g.feed(rms: 0.5, now: t0.addingTimeInterval(5.1)) // speech starts
        #expect(!started)
        #expect(g.heardSpeech)
        let brief = g.feed(rms: 0, now: t0.addingTimeInterval(5.5)) // quiet but brief
        #expect(!brief)
        let done = g.feed(rms: 0, now: t0.addingTimeInterval(6.2)) // 1.1s of quiet
        #expect(done)
    }

    @Test func silenceGateRespectsMinDuration() {
        let t0 = Date()
        var g = SilenceGate(threshold: 0.03, silenceTimeout: 0.1, minDuration: 10, start: t0)
        let started = g.feed(rms: 0.5, now: t0)
        #expect(!started)
        let early = g.feed(rms: 0, now: t0.addingTimeInterval(5)) // long quiet, too early
        #expect(!early)
        let done = g.feed(rms: 0, now: t0.addingTimeInterval(10.1))
        #expect(done)
    }

    @Test func sinkBoxFansOut() {
        let box = SinkBox()
        var a = 0, b = 0
        let ida = box.add { _ in a += 1 }
        _ = box.add { _ in b += 1 }
        let fmt = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!
        let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: 8)!
        box.consume(buf)
        #expect(a == 1 && b == 1)
        box.remove(ida)
        box.consume(buf)
        #expect(a == 1 && b == 2)
        #expect(box.count == 1)
    }
}

private extension VoiceSession {
    /// Tests always run with the toggle on (restored by the runner default).
    static func sharedWakeOn() { wakeEnabled = true }
}
