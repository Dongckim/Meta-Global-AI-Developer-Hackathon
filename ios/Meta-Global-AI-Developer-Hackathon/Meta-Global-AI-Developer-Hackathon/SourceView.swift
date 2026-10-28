import PDFKit
import SwiftUI

// MARK: - Original protocol files (this iPad only; the server never stores bytes)

enum OriginalStore {
    static func dir() -> URL {
        let d = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appending(path: "protocols", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    static func url(protocolId: String) -> URL {
        dir().appending(path: "\(protocolId).pdf")
    }

    static func save(_ data: Data, protocolId: String) {
        try? data.write(to: url(protocolId: protocolId), options: .atomic)
    }
}

/// "protocol:<id>#step:<n>" / "#liquid:<id>" / "#text:<phrase>" / bare "<id>".
/// Returns the protocol id plus the raw fragment ("" when none).
func parseSourceTarget(_ raw: String) -> (protocolId: String, fragment: String)? {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !t.isEmpty else { return nil }
    let parts = t.split(separator: "#", maxSplits: 1).map(String.init)
    var base = parts[0]
    if base.lowercased().hasPrefix("protocol:") { base = String(base.dropFirst(9)) }
    base = base.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !base.isEmpty else { return nil }
    return (base, parts.count > 1 ? parts[1] : "")
}

func stepNumber(fragment: String) -> Int? {
    let f = fragment.lowercased()
    guard f.hasPrefix("step:") else { return nil }
    return Int(f.dropFirst(5))
}

func textPhrase(fragment: String) -> String? {
    guard fragment.lowercased().hasPrefix("text:") else { return nil }
    let p = fragment.dropFirst(5).trimmingCharacters(in: .whitespacesAndNewlines)
    return p.isEmpty ? nil : p
}

/// Loose form for the second search pass: case, spaces, and units unified.
func loosePhrase(_ s: String) -> String {
    s.lowercased()
        .replacingOccurrences(of: "µ", with: "u")
        .replacingOccurrences(of: "μ", with: "u")
        .replacingOccurrences(of: "°", with: "")
        .split(whereSeparator: \.isWhitespace).joined(separator: " ")
}

struct PDFDocView: UIViewRepresentable {
    var url: URL
    var page: Int?
    var phrase: String?

    func makeUIView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.document = PDFDocument(url: url)
        return v
    }

    func updateUIView(_ view: PDFView, context: Context) {
        let key = "\(url.path)#\(page ?? 0)#\(phrase ?? "")"
        guard context.coordinator.lastKey != key else { return }
        context.coordinator.lastKey = key
        guard let doc = view.document else { return }
        if let n = page, let p = doc.page(at: n - 1) {
            view.go(to: p)
        }
        guard let phrase, !phrase.isEmpty else { return }
        let words = phrase.split(separator: " ")
        let tries = [phrase, loosePhrase(phrase),
                     words.prefix(8).joined(separator: " "),
                     words.prefix(4).joined(separator: " ")]
        for q in tries where !q.isEmpty {
            let hits = doc.findString(q, withOptions: [.caseInsensitive])
            if let first = hits.first, let dest = first.pages.first {
                view.go(to: dest)
                view.setCurrentSelection(first, animate: true)
                return
            }
        }
        // No hit: the page (or first page) still shows. Honest, not empty.
    }

    func makeCoordinator() -> Coord { Coord() }

    final class Coord {
        var lastKey = ""
    }
}

struct SourceView: View {
    var target: String
    var page: Int?
    var phrase: String?
    var title: String

    var body: some View {
        Group {
            if let parsed = parseSourceTarget(target),
               FileManager.default.fileExists(atPath: OriginalStore.url(protocolId: parsed.protocolId).path) {
                PDFDocView(url: OriginalStore.url(protocolId: parsed.protocolId),
                           page: page, phrase: phrase ?? textPhrase(fragment: parsed.fragment))
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "doc.questionmark").font(.largeTitle)
                        .foregroundStyle(.secondary)
                    Text("The original isn't on this iPad.")
                        .font(.headline)
                    Text("Import the protocol on this iPad to keep its PDF here.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
