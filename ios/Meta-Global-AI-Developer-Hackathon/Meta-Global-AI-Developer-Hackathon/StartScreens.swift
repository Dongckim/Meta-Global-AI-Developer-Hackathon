import PDFKit
import SwiftUI

// Start a run, ver 2: ⑤b Draft ready (Figma 37:13203), ⑤c Check the steps
// (37:13230), ⑤d Step source (37:13288), ⑤e Original page (37:13320),
// ⑤f Plan check (37:13351).
// Data: the snapshot draft (plan, review/errors, meta origin + original id +
// evidence text) and its server summary (draft.summary: robot rows with
// "volume · wells", flagged + reason, quote page; other steps; counts).
// Buttons are the registry's, filled from view.slots ("Say Step {step}").
// Server to do: value edits by voice (Fix it opens the tap editor for now).
// The same ⑤c-⑤e views show a finished run's final protocol (⑨0, no design
// of its own): ReviewSource carries the mode; what only the final protocol
// draws (plan → run, its changes) stays in FinalChanges / rowDetail.

/// Which review the ⑤c-⑤e views show.
enum ReviewMode { case importReview, finalProtocol }

/// What the review views draw: an import's draft, or a finished run's final
/// protocol (snapshot handoff), in the same summary shape.
struct ReviewSource {
    var meta: DraftMeta
    var summary: DraftSummary?
    var mode: ReviewMode

    init(_ draft: ProtocolDraft) { (meta, summary, mode) = (draft.meta, draft.summary, .importReview) }
    init(_ handoff: Handoff) { (meta, summary, mode) = (handoff.meta, handoff.summary, .finalProtocol) }
}

/// Where a draft's text came from, said honestly on every quote: the PDF,
/// the photo transcript, or the pasted text.
struct DraftOrigin {
    var icon: String
    var label: String
    /// "PDF" / "photo" / "text": what "Open ..." shows.
    var doc: String

    init(_ meta: DraftMeta) {
        switch meta.origin ?? "pdf" {
        case "photo": (icon, label, doc) = ("camera.fill", "photo transcript", "photo")
        case "text": (icon, label, doc) = ("text.alignleft", meta.source, "text")
        default: (icon, label, doc) = ("doc.fill", meta.source, "PDF")
        }
    }
}

/// Big centered title of a Start screen.
private struct StartTitle: View {
    var text: String
    var u: CGFloat
    var body: some View {
        Text(text).font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
            .foregroundStyle(Theme.text1).lineLimit(1).minimumScaleFactor(0.6)
    }
}

// MARK: - ⑤b Draft ready

struct DraftReadyScreen: View {
    var draft: ProtocolDraft
    var title: String
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        let s = draft.summary
        VStack(spacing: 36 * u) {
            VStack(spacing: 0) {
                Image(systemName: DraftOrigin(draft.meta).icon).font(Theme.ui(44 * u))
                    .foregroundStyle(Theme.polishBlue).enter(.pop, 0, u: u)
                StartTitle(text: "Draft ready", u: u).padding(.top, 24 * u).enter(.text, 1, u: u)
                Text(title).font(Theme.ui(28 * u)).foregroundStyle(Theme.text1).lineLimit(1)
                    .padding(.top, 18 * u).enter(.rise, 2, u: u)
                if let s {
                    Text(["\(s.conditions) conditions", "\(s.wells) wells", "\(s.steps) steps"].joined(separator: " · "))
                        .font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
                        .padding(.top, 22 * u).enter(.rise, 3, u: u)
                }
            }
            .padding(.vertical, 58 * u).frame(width: 756 * u)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 756 * u)
        }
        .frame(maxHeight: .infinity)
    }
}

// MARK: - ⑤c Check the steps

struct DraftStepsScreen: View {
    var review: ReviewSource
    var buttons: [ScreenButton]
    var u: CGFloat
    var onRow: (Int) -> Void
    var perform: (ScreenButton) -> Void

    /// Rows that fit without scrolling; the rest are counted.
    private let maxRows = 5

    var body: some View {
        let origin = DraftOrigin(review.meta)
        let rows = review.summary?.rows ?? []
        let final = review.mode == .finalProtocol
        VStack(spacing: 0) {
            StartTitle(text: final ? "Final protocol" : "Check the steps", u: u).enter(.text, 0, u: u)
            HStack(spacing: 12 * u) {
                Image(systemName: final ? "checklist" : origin.icon).foregroundStyle(Theme.polishBlue)
                Text(final ? (review.summary?.subtitle ?? "") : "From \(origin.label) · pipetting only")
                    .foregroundStyle(Theme.text2)
            }
            .font(Theme.ui(28 * u)).lineLimit(1).minimumScaleFactor(0.6)
            .padding(.top, 14 * u).enter(.rise, 1, u: u)
            VStack(spacing: 0) {
                ForEach(Array(rows.prefix(maxRows).enumerated()), id: \.element.id) { i, r in
                    row(r).enter(.rise, 2 + i, u: u)
                }
                if rows.count > maxRows {
                    Text("+\(rows.count - maxRows) more").font(Theme.ui(22 * u)).foregroundStyle(Theme.polishBlue)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 22 * u)
                }
                if let others = review.summary?.others, !others.isEmpty {
                    Line().stroke(Theme.text4, style: StrokeStyle(lineWidth: 1, dash: [4 * u, 4 * u]))
                        .frame(height: 1).padding(.top, 14 * u)
                    HStack(spacing: 16 * u) {
                        Text("Not for the robot")
                        Text(others.joined(separator: " · ")).lineLimit(1).minimumScaleFactor(0.6)
                        Spacer(minLength: 0)
                    }
                    .font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
                    .padding(.horizontal, 22 * u).frame(height: 64 * u)
                }
            }
            .padding(.horizontal, 26 * u).padding(.vertical, 18 * u)
            .frame(width: 1122 * u)
            .benchCard(radius: 44 * u)
            .padding(.top, 34 * u)
            if final {
                // the buttons share the steps card's width (1122), 36 apart: room for "Say Save without code"
                HStack(spacing: 36 * u) {
                    ForEach(Array(buttons.enumerated()), id: \.offset) { i, b in
                        SayButton(button: b, kind: i == 0 ? .primary : .secondary, size: 34 * u, perform: perform)
                            .frame(maxWidth: .infinity)
                    }
                }
                .frame(width: 1122 * u)
                .padding(.top, 30 * u)
            } else {
                SayRow(buttons: buttons, u: u, perform: perform).frame(width: 1122 * u).padding(.top, 30 * u)
            }
        }
        .frame(maxHeight: .infinity)
    }

    private func row(_ r: DraftRow) -> some View {
        Button { onRow(r.n) } label: {
            HStack(spacing: 0) {
                Text("\(r.n)").font(Theme.ui(22 * u, weight: .semibold))
                    .foregroundStyle(r.flagged ? .white : Theme.text1)
                    .frame(width: 44 * u, height: 44 * u)
                    .background(Circle().fill(r.flagged ? Theme.polishBlue : Theme.wellEmpty))
                Text(r.title).font(Theme.ui(30 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                    .lineLimit(1).minimumScaleFactor(0.6)
                    .frame(width: 270 * u, alignment: .leading).padding(.leading, 23 * u)
                Text(rowDetail(r)).font(Theme.ui(28 * u))
                    .foregroundStyle(changedRow(r) ? Theme.warning : Theme.text2)
                    .lineLimit(1).minimumScaleFactor(0.6)
                Spacer(minLength: 12 * u)
                if review.mode == .finalProtocol && !r.flagged {
                    EmptyView()  // as planned: no mark
                } else if r.flagged, !(r.reviewed ?? false) {
                    Text(review.mode == .finalProtocol ? "Changed" : "Check").font(Theme.ui(22 * u, weight: .semibold)).foregroundStyle(.white)
                        .padding(.horizontal, 22 * u).frame(height: 44 * u)
                        .background(Capsule().fill(Theme.polishBlue))
                } else {
                    Image(systemName: "checkmark").font(Theme.ui(18 * u, weight: .bold)).foregroundStyle(.white)
                        .frame(width: 40 * u, height: 40 * u).background(Circle().fill(Theme.polishBlue))
                }
            }
            .padding(.horizontal, 22 * u).frame(height: 84 * u)
            .background {
                if r.flagged { RoundedRectangle(cornerRadius: 24 * u, style: .continuous).fill(Theme.rowFocus) }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Step \(r.n), \(r.title)")
    }

    private func changedRow(_ r: DraftRow) -> Bool { review.mode == .finalProtocol && r.flagged }

    /// A changed final protocol step reads only what differs (server `delta`); every other row its detail.
    private func rowDetail(_ r: DraftRow) -> String {
        guard changedRow(r), let d = r.delta, !d.isEmpty else { return r.detail }
        return d
    }
}

/// A final protocol step's changes, each with the record event it came from.
/// Kept apart from the review layout: ⑨0 has no design yet and may change.
struct FinalChanges: View {
    var changes: [RunChange]
    var u: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 10 * u) {
            ForEach(changes, id: \.self) { c in
                HStack(alignment: .firstTextBaseline, spacing: 12 * u) {
                    Image(systemName: Self.icon(c.event)).foregroundStyle(Theme.polishBlue)
                    Text(c.text).foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.7)
                }
                .font(Theme.ui(22 * u))
            }
        }
    }

    /// Where the change came from in the record.
    static func icon(_ event: String) -> String {
        switch event {
        case "excluded": "xmark.circle"
        case "skipped": "forward"
        case "redo": "arrow.counterclockwise"
        case "done": "hand.tap"
        case "timer": "timer"
        default: "circle.dashed"
        }
    }
}

private struct Line: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in p.move(to: CGPoint(x: rect.minX, y: rect.midY)); p.addLine(to: CGPoint(x: rect.maxX, y: rect.midY)) }
    }
}

// MARK: - ⑤d Step source

struct DraftStepScreen: View {
    var review: ReviewSource
    var step: Int
    /// Registry buttons: Looks right, Fix it (the row), Open {doc} (inline).
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        let rows = review.summary?.rows ?? []
        let r = rows.first { $0.n == step }
        let origin = DraftOrigin(review.meta)
        let open = buttons.first { $0.action == "open_source_page" }
        VStack(spacing: 36 * u) {
            HStack(alignment: .top, spacing: 36 * u) {
                VStack(alignment: .leading, spacing: 4 * u) {
                    Text("Step \(step) of \(rows.count)").font(Theme.ui(28 * u)).foregroundStyle(Theme.text2)
                    Text(r?.title ?? "").font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                        .foregroundStyle(Theme.text1).lineLimit(1).minimumScaleFactor(0.6)
                    if review.mode == .finalProtocol, let r, let ch = r.changes, !ch.isEmpty {
                        Text(r.delta ?? "").font(Theme.ui(22 * u)).foregroundStyle(Theme.warning)
                            .lineLimit(2).minimumScaleFactor(0.7)
                        FinalChanges(changes: ch, u: u).padding(.top, 12 * u)
                    } else if let r, !r.detail.isEmpty {
                        Text(r.detail).font(Theme.ui(22 * u)).foregroundStyle(Theme.text2).lineLimit(1)
                    }
                }
                .padding(.horizontal, 52 * u)
                .frame(maxWidth: .infinity, minHeight: 236 * u, alignment: .leading)
                .benchCard(radius: 44 * u)
                .enter(.left, 0, u: u)
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 12 * u) {
                        Image(systemName: origin.icon).foregroundStyle(Theme.polishBlue)
                        Text([origin.label, r?.page.map { "page \($0)" }].compactMap { $0 }.joined(separator: " · "))
                            .foregroundStyle(Theme.text2)
                    }
                    .font(Theme.ui(22 * u)).lineLimit(1).minimumScaleFactor(0.6)
                    Text(r.map { "“\($0.quote)”" } ?? "").font(Theme.ui(22 * u)).foregroundStyle(Theme.text3)
                        .lineLimit(2).padding(.top, 16 * u)
                    Spacer(minLength: 12 * u)
                    if let open {
                        Button { perform(open) } label: {
                            (Say.prefix(Theme.polishBlue)
                             + Text(open.say).fontWeight(.semibold).foregroundStyle(Theme.text1)
                             + Text(" to see the page").foregroundStyle(Theme.text2))
                                .font(Theme.ui(22 * u))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 52 * u).padding(.vertical, 44 * u)
                .frame(maxWidth: .infinity, minHeight: 236 * u, alignment: .topLeading)
                .benchCard(radius: 44 * u)
                .enter(.right, 1, u: u)
            }
            .fixedSize(horizontal: false, vertical: true)
            SayRow(buttons: buttons.filter { $0.action != "open_source_page" }, u: u, perform: perform)
        }
        .frame(maxHeight: .infinity)
    }
}

// MARK: - ⑤e Original page

struct DraftPageScreen: View {
    var review: ReviewSource
    var step: Int
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    /// Next page, page 2, zoom, show the quote (registry control buttons).
    var commands: ScreenCommands
    @State private var paging = OriginalPaging(page: 1)

    private var row: DraftRow? { review.summary?.rows.first { $0.n == step } }
    private var oid: String { review.meta.originalId ?? "" }
    private var pdfURL: URL? {
        let u = OriginalStore.url(protocolId: oid)
        return review.meta.origin ?? "pdf" == "pdf" && !oid.isEmpty
            && FileManager.default.fileExists(atPath: u.path) ? u : nil
    }
    private var photos: [URL] { review.meta.origin == "photo" ? OriginalStore.photos(oid) : [] }
    private var pages: [String] {
        let e = review.meta.evidence
        return e?.pages ?? (e.map { [$0.text] } ?? [])
    }
    private var pageCount: Int {
        if let pdfURL { return PDFDocument(url: pdfURL)?.pageCount ?? 0 }
        return photos.isEmpty ? pages.count : photos.count
    }

    var body: some View {
        let origin = DraftOrigin(review.meta)
        let page = paging.page
        VStack(spacing: 24 * u) {
            VStack(spacing: 0) {
                HStack(spacing: 12 * u) {
                    Image(systemName: origin.icon).foregroundStyle(Theme.polishBlue)
                    Text(origin.label).foregroundStyle(Theme.text1).lineLimit(1)
                    Spacer(minLength: 0)
                    if pageCount > 0 { Text("Page \(page) of \(pageCount)").foregroundStyle(Theme.text2) }
                }
                .font(Theme.ui(22 * u))
                .padding(.horizontal, 40 * u).frame(height: 70 * u)
                .background(Theme.cardHead)
                content(page: page)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(width: 1060 * u, height: 540 * u)
            .clipShape(RoundedRectangle(cornerRadius: 44 * u, style: .continuous))
            .benchCard(radius: 44 * u)
            .enter(.rise, 0, u: u)
            ControlChips(buttons: buttons.filter { $0.action == "control" }, u: u, perform: perform)
            SayRow(buttons: buttons.filter { $0.action != "control" }, u: u, perform: perform).frame(width: 620 * u)
        }
        .frame(maxHeight: .infinity)
        .onAppear { paging = OriginalPaging(page: row?.page ?? 1) }
        .onChange(of: commands.gen) { _, _ in
            guard let c = commands.last else { return }
            _ = paging.apply(c.cmd, n: c.n, quotePage: row?.page ?? 1, count: max(pageCount, 1))
        }
    }

    @ViewBuilder private func content(page: Int) -> some View {
        if let pdfURL {
            PDFDocView(url: pdfURL, page: page, phrase: paging.onQuote ? row?.quote : nil, zoom: paging.zoom,
                       scroll: paging.scroll)
        } else if photos.indices.contains(page - 1), let img = UIImage(contentsOfFile: photos[page - 1].path) {
            // the photo is the original; the quote was checked against its transcript
            Image(uiImage: img).resizable().scaledToFit().scaleEffect(paging.zoom).padding(24 * u)
        } else {
            // pasted text, or a PDF that isn't on this iPad: the text it was read from
            Text(highlighted(pages.indices.contains(page - 1) ? pages[page - 1] : pages.joined(separator: "\n")))
                .font(Theme.ui(22 * u)).foregroundStyle(Theme.text1)
                .lineLimit(nil).minimumScaleFactor(0.5).scaleEffect(paging.zoom, anchor: .topLeading)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .padding(.horizontal, 84 * u).padding(.vertical, 40 * u)
        }
    }

    private func highlighted(_ text: String) -> AttributedString {
        var a = AttributedString(text)
        if let q = row?.quote, !q.isEmpty, let r = a.range(of: q, options: .caseInsensitive) {
            a[r].backgroundColor = Theme.polishBlue.opacity(0.18)
        }
        return a
    }
}

// MARK: - ⑤f Plan check

struct PlanCheckScreen: View {
    var draft: ProtocolDraft
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    @State private var plan = LabState()
    @State private var selected: WellSelect?

    var body: some View {
        let s = draft.summary
        HStack(alignment: .top, spacing: 36 * u) {
            PlateView(lab: plan, selected: $selected, showPlanned: true)
                .padding(28 * u)
                .frame(width: 800 * u, height: 566 * u)
                .benchCard(radius: 44 * u)
                .allowsHitTesting(false)
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 12 * u) {
                    Image(systemName: "checkmark")
                    Text("Draft ready")
                }
                .font(Theme.ui(28 * u)).foregroundStyle(Theme.text2).enter(.rise, 0, u: u)
                if let s {
                    Text("\(s.steps) steps").font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                        .foregroundStyle(Theme.text1).enter(.text, 1, u: u)
                    Text("\(s.conditions) conditions · \(s.wells) wells").font(Theme.ui(28 * u))
                        .foregroundStyle(Theme.text1).enter(.rise, 2, u: u)
                }
                Spacer(minLength: 24 * u)
                ForEach(Array(draft.plan.conditions.keys.sorted().enumerated()), id: \.element) { i, k in
                    let c = draft.plan.conditions[k]!
                    HStack(spacing: 14 * u) {
                        Circle().fill(Theme.groupTone(i)).overlay(Circle().strokeBorder(Theme.legendRing)).frame(width: 12 * u, height: 12 * u)
                        Text(c.label).foregroundStyle(Theme.text1)
                        Spacer(minLength: 0)
                        Text(wellRuns(c.wells)).foregroundStyle(Theme.text2)
                    }
                    .font(Theme.ui(28 * u)).lineLimit(1).minimumScaleFactor(0.6)
                    .frame(height: 50 * u)
                    .enter(.rise, 3 + i, u: u)
                }
                SayRow(buttons: buttons, u: u, perform: perform).padding(.top, 36 * u)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: Theme.Layout.benchHeight * u)
        }
        .padding(.top, Theme.Layout.benchTop * u)
        .onAppear { load() }
        .onChange(of: draft.plan.conditions.keys.sorted()) { _, _ in load() }
    }

    /// The draft's conditions on the plate (planned rings, nothing done).
    private func load() {
        plan.conditions = draft.plan.conditions.mapValues(PlanCondition.init)
    }
}

// MARK: - Import load pill (v36 `loadpill`)

/// "Reading" while an import runs, then "Draft ready ✓". The server doesn't
/// report progress, so the fill eases toward the end without a number and
/// only reaches it when the draft is in (v36 shows a timed %).
struct LoadPill: View {
    var done: Bool
    var u: CGFloat
    @State private var start = Date()

    var body: some View {
        TimelineView(.animation(paused: done)) { ctx in
            let t = ctx.date.timeIntervalSince(start)
            let p = done ? 1 : 0.92 * (1 - exp(-t / 8))
            GeometryReader { g in
                let w = max(360 * u, g.size.width * p)
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.wellEmpty)
                    HStack {
                        Text(done ? "Draft ready" : "Reading")
                        Spacer(minLength: 0)
                        if done { Image(systemName: "checkmark").fontWeight(.semibold) }
                    }
                    .font(Theme.ui(34 * u)).foregroundStyle(.white)
                    .padding(.horizontal, 48 * u)
                    .frame(width: w, height: g.size.height)
                    .background(Capsule().fill(Theme.polishBlue))
                    .animation(.easeOut(duration: 0.35), value: done)
                }
            }
        }
        .frame(width: 1248 * u, height: 96 * u)
        .accessibilityLabel(done ? "Draft ready" : "Reading the protocol")
    }
}
