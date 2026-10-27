import AVFoundation
import Foundation

/// One-shot speech: latest wins, never queues, never speaks while recording.
final class SpeechBus {
    static let shared = SpeechBus()

    private let synth = AVSpeechSynthesizer()

    var enabled: Bool {
        get { UserDefaults.standard.object(forKey: "ttsEnabled") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "ttsEnabled") }
    }

    func speak(_ text: String?) {
        guard enabled, let text, !text.isEmpty else { return }
        stop()
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: "en-US")
        synth.speak(u)
    }

    func stop() {
        if synth.isSpeaking || synth.isPaused {
            synth.stopSpeaking(at: .immediate)
        }
    }
}
