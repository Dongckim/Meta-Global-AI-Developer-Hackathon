import AVFoundation
import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct WakeMatcherTests {
    private let phrase = "Hey Milo"

    @Test func positives() {
        for t in ["hey milo", "Hey Milo", "HEY MILO!", "hey, milo", "say hey milo now",
                  "hey mylo", "hey my low", "a milo", "hey mila", "hay milo", "um hey milo please"] {
            #expect(WakeMatcher.matches(t, phrase: phrase), Comment(rawValue: t))
        }
    }

    @Test func negatives() {
        for t in ["", "hey", "milo", "milliliter", "mL", "mylar", "my lab", "hello",
                  "hey milliliter", "hey mylar", "hey my lab", "hello milo", "hey kilo",
                  "add 10 mL to C2", "this is a test"] {
            #expect(!WakeMatcher.matches(t, phrase: phrase), Comment(rawValue: t))
        }
    }

    @Test func soundKey() {
        #expect(WakeMatcher.key("my low") == "milo")
        #expect(WakeMatcher.key("Mylo") == "milo")
        #expect(WakeMatcher.key("milliliter") == "mililiter")
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
    var lastWordAt = Date.distantPast
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
final class FakeLive: LiveControlling {
    var onEvent: ((LiveEvent) -> Void)?
    var muted = false
    var fail = false
    var starts = 0
    var ends = 0
    func start(session: String) async throws {
        starts += 1
        if fail { throw LiveError.refused("test") }
    }
    func end() { ends += 1 }
    var audioRestarts = 0
    func restartAudio() async { audioRestarts += 1 }
    func send(_ ev: LiveEvent) { onEvent?(ev) }
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
                             speakMargin: TimeInterval = 5,
                             live: FakeLive? = nil,
                             ended: Flag? = nil,
                             firstClose: Bool = false) -> VoiceSession {
        VoiceSession.sharedWakeOn()
        let flag = chimed ?? Flag()
        return VoiceSession(wake: wake ?? FakeWake(), capture: capture ?? FakeCapture(),
                            speaker: speaker ?? FakeSpeaker(),
                            chime: { flag.value = true },
                            supportStub: { .ok },
                            micRequest: mic,
                            silenceTimeout: 0.05, minCapture: 0.02,
                            maxCapture: 5, awaitSpeech: 0.1, stuckTimeout: stuck,
                            speakMargin: speakMargin,
                            liveFactory: live.map { l in { l } },
                            endChime: { ended?.value = true },
                            closeDay: { _ in firstClose })
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
                let tap = V.admission(state: s, trigger: .orbTap, starting: starting)
                let label = Comment(rawValue: "\(s) starting=\(starting)")
                switch s {
                case .ready:
                    #expect(wake == (starting ? .ignore : .accept), label)
                    #expect(manual == (starting ? .ignore : .accept), label)
                    #expect(tap == (starting ? .ignore : .accept), label)
                case .speaking:
                    #expect(wake == .ignore, label)
                    #expect(manual == .bargeIn, label)
                    #expect(tap == .interrupt, label)
                default:
                    #expect(wake == .ignore, label)
                    #expect(manual == .ignore, label)
                    #expect(tap == .ignore, label)
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
        let wake = Persona.current.wakePhrase.lowercased() // latch uses the configured phrase
        var latch = WakeLatch()
        #expect(latch.hear(wake))
        // rejected: fresh task, nothing carried -> the next wake phrase offers again
        latch.newTask(carry: nil)
        #expect(!latch.hear("show c5")) // stale phrase gone
        #expect(latch.hear(wake))
        latch.accept()
        #expect(!latch.hear("\(wake) \(wake)")) // accepted: held until a new task
        let words = wake.split(separator: " ")
        latch.newTask(carry: "um " + words[0])
        #expect(latch.hear(words.dropFirst().joined(separator: " "))) // split across the boundary
    }

    @Test func wakeProbeReportsRate() {
        var p = WakeProbe(target: 3)
        #expect(p.note("hey milo", hit: true) == nil)
        #expect(p.note("hey my lab", hit: false) == nil)
        let s = p.note("hey mylo", hit: true)
        #expect(s?.hasPrefix("measure done: 2/3 (66%)") == true)
        #expect(s?.contains("✗ \"hey my lab\"") == true)
    }

    @Test func liveSessionTurnFlow() async throws {
        let wake = FakeWake(), speaker = FakeSpeaker(), live = FakeLive()
        let voice = makeSession(wake: wake, speaker: speaker, live: live)
        voice.activate()
        #expect(await settle { wake.running })
        #expect(wake.fire()) // wake opens a live session, not a capture
        #expect(await settle { voice.inSession && voice.state == .listening })
        #expect(!wake.running && live.starts == 1)
        live.send(.partial("mark C2"))
        #expect(voice.state == .transcribing && voice.partialText == "mark C2")
        let r = try JSONDecoder().decode(VoiceResponse.self, from: Data(#"""
            {"transcript": "mark C2 done", "tool_calls": [], "messages": [], "reply": "Nice.",
             "ms": {}, "cards": [], "record_version": 1, "sources": [], "speak": "Nice."}
            """#.utf8))
        live.send(.turn(r, endSession: false))
        #expect(voice.liveTurnGen == 1 && voice.liveTurn?.transcript == "mark C2 done" && voice.partialText == nil)
        voice.didRespond(hasSources: false)
        voice.speak("Nice.")
        #expect(voice.state == .speaking && live.muted) // silence while the agent talks
        speaker.complete()
        #expect(voice.state == .listening && !live.muted) // back to the open session
        #expect(!wake.running) // wake stays off during the session
        live.send(.closed(why: "window"))
        #expect(voice.state == .ready && !voice.inSession)
        #expect(await settle { wake.running }) // wake resumes after the session
    }

    @Test func longSpeechInSessionIsNeverCutAndStallsKeepTheSession() async {
        let wake = FakeWake(), speaker = FakeSpeaker(), live = FakeLive()
        let voice = makeSession(wake: wake, speaker: speaker, speakMargin: 0.3, live: live)
        await openSession(voice, wake)
        voice.speak(String(repeating: "C2 has bubbles. ", count: 40)) // a long list
        for _ in 0..<6 { // words keep coming for ~0.9 s, longer than the margin
            try? await Task.sleep(for: .milliseconds(150))
            speaker.lastWordAt = Date()
        }
        #expect(voice.state == .speaking && voice.inSession) // progressing: not cut
        // the voice stalls: recover to listening, the session stays open
        #expect(await settle { voice.state == .listening })
        #expect(voice.inSession && live.ends == 0 && !live.muted && speaker.stops >= 1)
    }

    @Test func audioLossInSessionReattachesInsteadOfClosing() async {
        let wake = FakeWake(), live = FakeLive()
        let voice = makeSession(wake: wake, live: live)
        await openSession(voice, wake)
        voice.audioLost()
        #expect(await settle { live.audioRestarts == 1 })
        #expect(voice.inSession && voice.state == .listening && live.ends == 0)
    }

    private func openSession(_ voice: VoiceSession, _ wake: FakeWake) async {
        voice.activate()
        _ = await settle { wake.running }
        wake.fire()
        _ = await settle { voice.inSession }
    }

    @Test func windowCloseChimesAndSaysFirstCloseLineOnce() async {
        let wake = FakeWake(), speaker = FakeSpeaker(), live = FakeLive(), ended = Flag()
        let voice = makeSession(wake: wake, speaker: speaker, live: live, ended: ended, firstClose: true)
        await openSession(voice, wake)
        #expect(voice.edgeFade == Theme.edgeFade)
        live.send(.closed(why: "window"))
        #expect(ended.value && voice.edgeFade == Theme.edgeSessionFade) // sound + 1 s fade
        #expect(speaker.texts == [Persona.live.firstCloseLine]) // the day's first close
    }

    @Test func laterClosesAndExplicitEndStayQuiet() async {
        let wake = FakeWake(), speaker = FakeSpeaker(), live = FakeLive(), ended = Flag()
        let voice = makeSession(wake: wake, speaker: speaker, live: live, ended: ended, firstClose: false)
        await openSession(voice, wake)
        live.send(.closed(why: "window"))
        #expect(ended.value && speaker.texts.isEmpty) // not the day's first close
        let wake2 = FakeWake(), speaker2 = FakeSpeaker(), live2 = FakeLive(), ended2 = Flag()
        let voice2 = makeSession(wake: wake2, speaker: speaker2, live: live2, ended: ended2, firstClose: true)
        await openSession(voice2, wake2)
        live2.send(.closed(why: "end_session")) // "thanks" already got its reply
        #expect(ended2.value && speaker2.texts.isEmpty)
    }

    @Test func brokenStreamFallsBackForAWhile() async {
        let wake = FakeWake(), capture = FakeCapture(), live = FakeLive(), ended = Flag()
        let voice = makeSession(wake: wake, capture: capture, live: live, ended: ended)
        await openSession(voice, wake)
        live.send(.closed(why: "error"))
        #expect(!ended.value && voice.aside == Persona.live.fallbackLine && !voice.inSession)
        #expect(await settle { wake.running })
        capture.script = [0.5, 0.5, 0, 0, 0, 0]
        wake.fire()
        #expect(await settle { voice.state == .transcribing }) // per-utterance capture now
        #expect(live.starts == 1 && capture.starts == 1)
    }

    @Test func liveIgnoredShowsAsideAndKeepsListening() async {
        let wake = FakeWake(), live = FakeLive()
        let voice = makeSession(wake: wake, live: live)
        voice.activate()
        #expect(await settle { wake.running })
        wake.fire()
        #expect(await settle { voice.inSession })
        live.send(.partial("are we meeting"))
        live.send(.ignored(line: "Another voice — ignored"))
        #expect(voice.state == .listening && voice.aside == "Another voice — ignored" && voice.partialText == nil)
        #expect(voice.liveTurnGen == 0)
        #expect(await settle(timeout: 3) { voice.aside == nil }) // ~1.5 s
    }

    @Test func orbTapOpensSessionInterruptsSpeechAndIgnoresIdle() async {
        let wake = FakeWake(), speaker = FakeSpeaker(), live = FakeLive(), chimed = Flag()
        let voice = makeSession(wake: wake, speaker: speaker, chimed: chimed, live: live)
        voice.activate()
        #expect(await settle { wake.running })
        #expect(voice.open(trigger: .orbTap)) // like the wake word
        #expect(await settle { voice.inSession && voice.state == .listening })
        #expect(chimed.value && live.starts == 1)
        #expect(!voice.open(trigger: .orbTap)) // session idle: already listening
        voice.speak("A long answer.")
        #expect(voice.open(trigger: .orbTap)) // tap while speaking: stop talking
        #expect(speaker.stops == 1 && voice.state == .listening && voice.inSession && !live.muted)
        speaker.complete() // a late TTS done changes nothing
        #expect(voice.state == .listening && live.starts == 1)
    }

    @Test func orbTapFallsBackToOneUtterance() async {
        let wake = FakeWake(), capture = FakeCapture(), live = FakeLive()
        live.fail = true
        capture.script = [0.5, 0.5, 0, 0, 0, 0]
        let voice = makeSession(wake: wake, capture: capture, live: live)
        voice.activate()
        #expect(await settle { wake.running })
        voice.open(trigger: .orbTap)
        #expect(await settle { voice.state == .transcribing }) // auto-ended, not held
        #expect(!voice.inSession && capture.starts == 1)
    }

    @Test func orbTapOutsideSessionInterruptsToReady() async {
        let wake = FakeWake(), speaker = FakeSpeaker()
        let voice = makeSession(wake: wake, speaker: speaker)
        voice.activate()
        voice.speak("talking")
        #expect(voice.open(trigger: .orbTap))
        #expect(voice.state == .ready && speaker.stops == 1)
        #expect(await settle { wake.running })
    }

    @Test func liveStartFailureFallsBackToCapture() async {
        let wake = FakeWake(), capture = FakeCapture(), live = FakeLive()
        live.fail = true
        capture.script = [0.5, 0.5, 0, 0, 0, 0]
        let voice = makeSession(wake: wake, capture: capture, live: live)
        voice.activate()
        #expect(await settle { wake.running })
        wake.fire()
        #expect(await settle { voice.state == .transcribing }) // per-utterance path ran
        #expect(!voice.inSession && capture.starts == 1)
    }

    @Test func pcmFramerMakes80msFrames() {
        var f = PCMFramer()
        let samples: [Float] = Array(repeating: 0.5, count: 1280 + 100) + [2, -2]
        let out = samples.withUnsafeBufferPointer { f.append($0, muted: false) }
        #expect(out.count == 1 && out[0].count == 2560)
        let v = out[0].withUnsafeBytes { $0.load(as: Int16.self) }
        #expect(Int16(littleEndian: v) == 16383)
        let more: [Float] = Array(repeating: 1, count: 1280)
        let quiet = more.withUnsafeBufferPointer { f.append($0, muted: true) }
        #expect(quiet.count == 1)
        // the carried-over 102 samples (clamped to ±1) come first, then silence
        let first = quiet[0].withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        #expect(first[100] == 32767 && first[101] == -32767 && first[102] == 0 && first.last == 0)
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
