import AVFoundation
import os

/// Unified-log categories. Filter Console with
/// subsystem:com.dongchankim.Meta-Global-AI-Developer-Hackathon .
enum Log {
    static let dat = Logger(subsystem: Subsystem.id, category: "dat")
    static let wake = Logger(subsystem: Subsystem.id, category: "wake")
    static let voice = Logger(subsystem: Subsystem.id, category: "voice")
    static let audio = Logger(subsystem: Subsystem.id, category: "audio")
    static let view = Logger(subsystem: Subsystem.id, category: "view")

    /// Session category/mode and live route, for [audio] lines.
    static func route() -> String {
        #if os(iOS)
        let s = AVAudioSession.sharedInstance()
        let ins = s.currentRoute.inputs.map { "\($0.portName)(\($0.portType.rawValue))" }
        let outs = s.currentRoute.outputs.map { "\($0.portName)(\($0.portType.rawValue))" }
        return "cat=\(s.category.rawValue) mode=\(s.mode.rawValue) in=\(ins) out=\(outs) pref=\(s.preferredInput?.portName ?? "nil")"
        #else
        return "-"
        #endif
    }
}

private enum Subsystem {
    static let id = "com.dongchankim.Meta-Global-AI-Developer-Hackathon"
}
