import AVFoundation
import Foundation
import os

/// Installed-voice facts the chooser needs (plain values, so it is testable).
struct VoiceCandidate: Equatable {
    var id: String
    var name: String
    var language: String
    var quality: AVSpeechSynthesisVoiceQuality
}

/// One-shot speech: latest wins, never queues, never speaks while recording.
/// Voice, rate and pitch come only from `Persona` (pip_voice.json).
final class SpeechBus: NSObject, AVSpeechSynthesizerDelegate {
    static let shared = SpeechBus()

    private let synth = AVSpeechSynthesizer()
    private var completions: [() -> Void] = []
    private var pendingUtterances = 0
    private var suppressCancel = false

    private override init() {
        super.init()
        synth.delegate = self
    }

    var enabled: Bool {
        get { UserDefaults.standard.object(forKey: "ttsEnabled") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "ttsEnabled") }
    }

    static func qualityName(_ q: AVSpeechSynthesisVoiceQuality) -> String {
        switch q {
        case .premium: return "premium"
        case .enhanced: return "enhanced"
        default: return "default"
        }
    }

    /// Persona voice by name and quality; else the same name enhanced; else a
    /// premium voice in the same language (the named voice's, else the
    /// persona's); else nil = system default. Never by identifier.
    static func choose(_ voices: [VoiceCandidate], persona: Persona) -> (id: String, reason: String)? {
        let named = voices.filter { $0.name == persona.voice }
        if let v = named.first(where: { qualityName($0.quality) == persona.quality }) {
            return (v.id, "\(persona.voice) \(persona.quality)")
        }
        if let v = named.first(where: { $0.quality == .enhanced }) {
            return (v.id, "\(persona.voice) enhanced (no \(persona.quality))")
        }
        let lang = named.first?.language ?? persona.language
        if let v = voices.filter({ $0.language == lang && $0.quality == .premium })
            .min(by: { $0.name < $1.name }) {
            return (v.id, "\(v.name) premium \(lang) (no \(persona.voice))")
        }
        return nil
    }

    /// Picked once per launch and logged; the diagnostics show `voiceInfo`.
    private(set) lazy var voice: AVSpeechSynthesisVoice? = {
        let installed = AVSpeechSynthesisVoice.speechVoices()
        let cands = installed.map {
            VoiceCandidate(id: $0.identifier, name: $0.name, language: $0.language, quality: $0.quality)
        }
        let p = Persona.current
        if let pick = Self.choose(cands, persona: p),
           let v = installed.first(where: { $0.identifier == pick.id }) {
            voiceReason = pick.reason
            Log.voice.info("[voice] tts voice \(v.name, privacy: .public) \(v.language, privacy: .public) \(Self.qualityName(v.quality), privacy: .public) (\(pick.reason, privacy: .public))")
            return v
        }
        let v = AVSpeechSynthesisVoice(language: p.language)
        voiceReason = "system default \(p.language) (no \(p.voice), no premium)"
        Log.voice.info("[voice] tts voice \(v?.name ?? "none", privacy: .public) (\(self.voiceReason, privacy: .public))")
        return v
    }()

    private(set) var voiceReason = ""

    var voiceInfo: String {
        guard let v = voice else { return "none" }
        return "\(v.name) · \(v.language) · \(Self.qualityName(v.quality)) — \(voiceReason)"
    }

    /// Speaks, then runs onDone exactly once (natural end or stop).
    /// Disabled/empty speaks nothing and completes immediately.
    func speak(_ text: String?, onDone: @escaping () -> Void = {}) {
        guard enabled, let text, !text.isEmpty else { onDone(); return }
        interrupt()
        completions = [onDone]
        let chunks = Self.sentences(text)
        pendingUtterances = chunks.count
        for chunk in chunks {
            let u = AVSpeechUtterance(string: chunk)
            u.voice = voice
            u.rate = Float(Persona.current.rate)
            u.pitchMultiplier = Float(Persona.current.pitch)
            u.postUtteranceDelay = 0.12
            synth.speak(u)
        }
    }

    /// TTS holds the audio session (MicEngine won't deactivate under it).
    var isSpeaking: Bool { synth.isSpeaking || synth.isPaused }

    func stop() {
        if synth.isSpeaking || synth.isPaused {
            synth.stopSpeaking(at: .immediate)
        }
    }

    /// Replaces in-flight speech without completing it (interrupted, not done).
    private func interrupt() {
        guard synth.isSpeaking || synth.isPaused else { return }
        suppressCancel = true
        synth.stopSpeaking(at: .immediate)
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        pendingUtterances -= 1
        if pendingUtterances <= 0 { fireCompletions() }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        if suppressCancel { suppressCancel = false; return }
        fireCompletions()
    }

    private func fireCompletions() {
        pendingUtterances = 0
        let cs = completions
        completions = []
        for c in cs { c() }
    }

    private static func sentences(_ text: String) -> [String] {
        let parts = text.components(separatedBy: CharacterSet(charactersIn: ".?!"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return parts.isEmpty ? [text] : parts
    }
}
