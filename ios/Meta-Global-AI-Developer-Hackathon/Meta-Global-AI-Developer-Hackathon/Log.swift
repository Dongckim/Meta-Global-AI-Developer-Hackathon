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
    static let imports = Logger(subsystem: Subsystem.id, category: "import")
    /// Launch to Main, one line per stage with ms since launch: "[boot] 412ms sse open".
    static let boot = Logger(subsystem: Subsystem.id, category: "boot")
    /// Screen geometry while it settles: "[layout] main 1376x1032 safe(t0 b0) u=1.00".
    static let layout = Logger(subsystem: Subsystem.id, category: "layout")

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

/// Launch stages on one clock (ms since the app struct was made), so the
/// splash stalls show as gaps between lines.
enum Boot {
    static let t0 = Date()
    static var ms: Int { Int(Date().timeIntervalSince(t0) * 1000) }

    static func mark(_ stage: String) {
        Log.boot.info("[boot] \(ms)ms \(stage, privacy: .public)")
    }

    /// A synchronous step on the calling thread, with how long it blocked.
    @discardableResult
    static func time<T>(_ stage: String, _ body: () throws -> T) rethrows -> T {
        let start = Date()
        defer {
            let took = Int(Date().timeIntervalSince(start) * 1000)
            Log.boot.info("[boot] \(ms)ms \(stage, privacy: .public) took=\(took)ms main=\(Thread.isMainThread)")
        }
        return try body()
    }
}

private enum Subsystem {
    static let id = "com.dongchankim.Meta-Global-AI-Developer-Hackathon"
}
