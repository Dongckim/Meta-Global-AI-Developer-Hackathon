import Foundation
import os

/// Shown/spoken identity, read only from the bundled shared/pip_voice.json
/// `persona` block (the server reads the same file). Every user-facing
/// name, the wake phrase and the TTS voice come from here.
struct Persona: Decodable, Equatable {
    var agentName: String
    var wakePhrase: String
    var voice: String
    var quality: String
    var language: String
    var rate: Double
    var pitch: Double

    enum CodingKeys: String, CodingKey {
        case agentName = "agent_name", wakePhrase = "wake_phrase", voice, quality, language, rate, pitch
    }

    static let current: Persona = load(from: Bundle.main.url(forResource: "pip_voice", withExtension: "json"))

    /// Live conversation lines (pip_voice.json `live`).
    struct Live: Decodable, Equatable {
        var firstCloseLine: String
        var fallbackLine: String
        enum CodingKeys: String, CodingKey {
            case firstCloseLine = "first_close_line", fallbackLine = "fallback_line"
        }
    }

    static let live: Live = {
        struct File: Decodable { var live: Live }
        guard let url = Bundle.main.url(forResource: "pip_voice", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let file = try? JSONDecoder().decode(File.self, from: data) else {
            Log.voice.error("[voice] live lines missing in pip_voice.json")
            return Live(firstCloseLine: "", fallbackLine: "")
        }
        return file.live
    }()

    /// Neutral stand-in when the bundled file is missing or broken (logged).
    static let fallback = Persona(agentName: "Assistant", wakePhrase: "", voice: "", quality: "premium",
                                  language: "en-US", rate: 0.52, pitch: 0.92)

    static func load(from url: URL?) -> Persona {
        struct File: Decodable { var persona: Persona }
        guard let url, let data = try? Data(contentsOf: url),
              let file = try? JSONDecoder().decode(File.self, from: data) else {
            Log.voice.error("[voice] persona missing in pip_voice.json; using fallback")
            return fallback
        }
        return file.persona
    }
}
