import PDFKit
import SwiftUI

// MARK: - Original protocol files (this iPad only; the server never stores bytes)

/// PDFs and page photos live here under the import's original id (the server
/// keeps that id with the draft and the protocol). Protocols imported before
/// original ids keep their file under the protocol id.
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

    static func photoURL(_ id: String, page: Int) -> URL {
        dir().appending(path: "\(id)-p\(page).jpg")
    }

    /// Page photos of an import, in page order.
    static func photos(_ id: String) -> [URL] {
        guard !id.isEmpty else { return [] }
        return Array((1...200).lazy.map { photoURL(id, page: $0) }
            .prefix { FileManager.default.fileExists(atPath: $0.path) })
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
    /// Highlight this text (show the quote); nil while paging by hand.
    var phrase: String?
    /// 1 = fit the page; zoom in / out multiply it.
    var zoom: CGFloat = 1

    func makeUIView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.document = PDFDocument(url: url)
        return v
    }

    func updateUIView(_ view: PDFView, context: Context) {
        if context.coordinator.lastZoom != zoom {
            context.coordinator.lastZoom = zoom
            view.autoScales = zoom == 1
            if zoom != 1 { view.scaleFactor = view.scaleFactorForSizeToFit * zoom }
        }
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
        var lastZoom: CGFloat = 1
    }
}

/// Paging, zoom and "show the quote" for an original (⑤e and the in-run
/// original), driven by the screen's registry buttons (control commands).
struct OriginalPaging: Equatable {
    var page: Int
    var zoom: CGFloat = 1
    /// true: the quote's page with it highlighted; false: paged by hand.
    var onQuote = true

    /// Applies one control command; `count` pages exist. False when it isn't one.
    mutating func apply(_ cmd: String, n: Int?, quotePage: Int, count: Int) -> Bool {
        let last = max(count, 1)
        switch cmd {
        case "next_page": page = min(page + 1, last); onQuote = false
        case "previous_page": page = max(page - 1, 1); onQuote = false
        case "page": page = min(max(n ?? page, 1), last); onQuote = false
        case "quote": page = quotePage; onQuote = true; zoom = 1
        case "zoom_in": zoom = min(zoom * 1.5, 4)
        case "zoom_out": zoom = max(zoom / 1.5, 1)
        default: return false
        }
        return true
    }
}

struct SourceView: View {
    var target: String
    var page: Int?
    var phrase: String?
    var title: String
    /// The screen's control commands (next page, zoom, show the quote).
    var commands: ScreenCommands? = nil
    @State private var paging = OriginalPaging(page: 1)

    var body: some View {
        Group {
            if let parsed = parseSourceTarget(target),
               FileManager.default.fileExists(atPath: OriginalStore.url(protocolId: parsed.protocolId).path) {
                let url = OriginalStore.url(protocolId: parsed.protocolId)
                PDFDocView(url: url, page: paging.page,
                           phrase: paging.onQuote ? (phrase ?? textPhrase(fragment: parsed.fragment)) : nil,
                           zoom: paging.zoom)
                    .onChange(of: commands?.gen) { _, _ in
                        guard let c = commands?.last else { return }
                        _ = paging.apply(c.cmd, n: c.n, quotePage: page ?? 1,
                                         count: PDFDocument(url: url)?.pageCount ?? 1)
                    }
            } else if let parsed = parseSourceTarget(target),
                      case let photos = OriginalStore.photos(parsed.protocolId), !photos.isEmpty,
                      let img = UIImage(contentsOfFile: photos[min(max(paging.page - 1, 0), photos.count - 1)].path) {
                // a photo import: the page photo is the original
                Image(uiImage: img).resizable().scaledToFit().scaleEffect(paging.zoom).padding()
                    .onChange(of: commands?.gen) { _, _ in
                        guard let c = commands?.last else { return }
                        _ = paging.apply(c.cmd, n: c.n, quotePage: page ?? 1, count: photos.count)
                    }
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
        .onAppear { paging = OriginalPaging(page: page ?? 1) }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }
}
