import Foundation

/// "Hey Pip" detector over on-device transcripts. One pure function so the
/// iPad behavior and the unit tests share it exactly.
enum WakeMatcher {
    /// Hey-variants the recognizer emits.
    private static let callers: Set<String> = ["hey", "hay"]
    /// Pip-variants incl. common mishearings. "pipe"/"pipette" are NOT here:
    /// whole-token compare means they can never match.
    private static let cores: Set<String> = ["pip", "pep", "peep"]

    /// True when any adjacent word pair is caller + core ("say hey pip now"
    /// matches; "pipette the sample" and "pass the pipe" do not).
    static func matches(_ transcript: String) -> Bool {
        let words = transcript.lowercased().split { !$0.isLetter }.map(String.init)
        guard words.count >= 2 else { return false }
        for i in 0..<(words.count - 1) {
            if callers.contains(words[i]), cores.contains(words[i + 1]) {
                return true
            }
        }
        return false
    }
}
