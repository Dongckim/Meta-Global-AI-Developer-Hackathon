import os

/// Unified-log categories. Filter Console with
/// subsystem:com.dongchankim.Meta-Global-AI-Developer-Hackathon .
enum Log {
    static let dat = Logger(subsystem: Subsystem.id, category: "dat")
    static let wake = Logger(subsystem: Subsystem.id, category: "wake")
    static let voice = Logger(subsystem: Subsystem.id, category: "voice")
}

private enum Subsystem {
    static let id = "com.dongchankim.Meta-Global-AI-Developer-Hackathon"
}
