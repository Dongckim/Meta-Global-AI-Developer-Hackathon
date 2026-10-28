import AVFoundation
import Foundation

/// One-shot speech: latest wins, never queues, never speaks while recording.
/// Voice: user pick, else best installed en-US (Ava premium first). Voice and
/// rate persist in UserDefaults.
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

    var rate: Double {
        get { UserDefaults.standard.object(forKey: "pipVoiceRate") as? Double ?? 0.52 }
        set { UserDefaults.standard.set(newValue, forKey: "pipVoiceRate") }
    }

    /// Explicit user pick; nil means automatic (best first).
    var voiceID: String? {
        get { UserDefaults.standard.string(forKey: "pipVoiceId") }
        set { UserDefaults.standard.set(newValue, forKey: "pipVoiceId") }
    }

    /// Installed en-US voices, best first.
    static func enUSVoices() -> [AVSpeechSynthesisVoice] {
        AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language == "en-US" }
            .sorted {
                let (a, b) = (rank(name: $0.name, quality: $0.quality),
                              rank(name: $1.name, quality: $1.quality))
                return a == b ? $0.identifier < $1.identifier : a < b
            }
    }

    /// Ava premium → Ava enhanced → other premium → enhanced → default.
    static func rank(name: String, quality: AVSpeechSynthesisVoiceQuality) -> Int {
        let ava = name == "Ava"
        switch quality {
        case .premium: return ava ? 0 : 2
        case .enhanced: return ava ? 1 : 3
        default: return 4
        }
    }

    static func qualityName(_ q: AVSpeechSynthesisVoiceQuality) -> String {
        switch q {
        case .premium: return "premium"
        case .enhanced: return "enhanced"
        default: return "default"
        }
    }

    var voice: AVSpeechSynthesisVoice? {
        let list = Self.enUSVoices()
        if let id = voiceID, let v = list.first(where: { $0.identifier == id }) { return v }
        return list.first ?? AVSpeechSynthesisVoice(language: "en-US")
    }

    var voiceInfo: String {
        guard let v = voice else { return "none" }
        return "\(v.name) · \(v.identifier) · \(Self.qualityName(v.quality))"
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
            u.rate = Float(rate)
            u.pitchMultiplier = 0.92
            u.postUtteranceDelay = 0.12
            synth.speak(u)
        }
    }

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
