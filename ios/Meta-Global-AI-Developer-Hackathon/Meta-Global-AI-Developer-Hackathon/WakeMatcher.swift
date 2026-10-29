import Foundation

/// Wake-phrase detector over on-device transcripts, by sound instead of a
/// word list. One pure function so the iPad behavior and the unit tests
/// share it exactly. The phrase comes from `Persona.wakePhrase`
/// ("Hey Milo" = caller "hey" + name "milo").
enum WakeMatcher {
    /// True when a caller-like word ("hey", "hay", "a", "hi") is followed by
    /// one or two words that sound like the name ("milo", "my low", "mila").
    /// The name alone, or a caller with a different-sounding word, is not a wake.
    static func matches(_ transcript: String, phrase: String = Persona.current.wakePhrase) -> Bool {
        let target = words(phrase)
        guard target.count >= 2 else { return false }
        let name = key(target.dropFirst().joined())
        let words = words(transcript)
        guard words.count >= 2 else { return false }
        for i in 0..<(words.count - 1) where isCaller(words[i]) {
            let one = key(words[i + 1])
            let two = i + 2 < words.count ? key(words[i + 1] + words[i + 2]) : ""
            if soundsLike(one, name) || (!two.isEmpty && soundsLike(two, name)) { return true }
        }
        return false
    }

    private static func words(_ s: String) -> [String] {
        s.lowercased().split { !$0.isLetter }.map(String.init)
    }

    /// "hey"/"hay"/"a"/"eh"/"hi": optional h, then only vowel sounds.
    private static func isCaller(_ w: String) -> Bool {
        let rest = w.hasPrefix("h") ? String(w.dropFirst()) : w
        return !rest.isEmpty && rest.count <= 3 && rest.allSatisfy { "aeiy".contains($0) }
    }

    /// Sound key: y as a vowel is i ("my" -> "mi"), "ow"/"oh" ending in o
    /// ("low" -> "lo"), doubled letters collapsed ("milliliter" -> "mililiter").
    static func key(_ s: String) -> String {
        var k = s.lowercased().filter(\.isLetter).replacingOccurrences(of: "y", with: "i")
        for (from, to) in [("ow", "o"), ("oh", "o")] { k = k.replacingOccurrences(of: from, with: to) }
        var out = ""
        for c in k where out.last != c { out.append(c) }
        return out
    }

    /// Same first sound and at most one edit apart (two for long names), so
    /// "mila" matches "milo" while "mylar", "my lab" and "kilo" do not.
    private static func soundsLike(_ a: String, _ b: String) -> Bool {
        guard let fa = a.first, fa == b.first else { return false }
        return distance(a, b) <= (b.count > 5 ? 2 : 1)
    }

    private static func distance(_ a: String, _ b: String) -> Int {
        let a = Array(a), b = Array(b)
        guard !a.isEmpty, !b.isEmpty else { return max(a.count, b.count) }
        var row = Array(0...b.count)
        for i in 1...a.count {
            var prev = row[0]
            row[0] = i
            for j in 1...b.count {
                let cur = row[j]
                row[j] = min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] == b[j - 1] ? 0 : 1))
                prev = cur
            }
        }
        return row[b.count]
    }
}
