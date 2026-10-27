import AVFoundation
import Foundation

/// One-shot speech: latest wins, never queues, never speaks while recording.
/// Voice: user pick, else best installed en-US (Ava premium first). Voice and
/// rate persist in UserDefaults.
final class SpeechBus {
    static let shared = SpeechBus()

    private let synth = AVSpeechSynthesizer()

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

    func speak(_ text: String?) {
        guard enabled, let text, !text.isEmpty else { return }
        stop()
        for chunk in Self.sentences(text) {
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

    private static func sentences(_ text: String) -> [String] {
        let parts = text.components(separatedBy: CharacterSet(charactersIn: ".?!"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return parts.isEmpty ? [text] : parts
    }
}
