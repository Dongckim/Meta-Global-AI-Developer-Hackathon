import SwiftUI

// Web search, ver 2: ⑧ Web search answer (Figma 37:14048) and ⓪c Home ·
// Web searches (37:13104). Data: GET /searches (server/search.py keeps every
// search sent; the list shows the answered ones with their cited sources).
// Buttons are the registry's (web_answer: Open source / Source 2 / Back;
// web_searches: Back); a row tap opens that search like saying it.

// MARK: - ⑧ Web search answer

struct WebAnswerScreen: View {
    private typealias L = Theme.Layout
    var item: WebSearch
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    /// A photo question: what the glasses see while it waits (their stream),
    /// the photo they just took (shown at once, read meanwhile); Take it asks for one.
    var live: UIImage? = nil
    var shot: UIImage? = nil
    var commands: ScreenCommands? = nil
    /// The glasses' stream runs while "Show me." is up (true on, false off).
    var onLive: (Bool) -> Void = { _ in }
    var onTake: () -> Void = {}

    /// pending: opened when the search started (a photo question: waiting for
    /// its photo, or reading it); the answer fills in on done.
    private var pending: Bool { ["pending", "looking", "reading"].contains(item.status ?? "") }
    private var failed: Bool { !pending && item.status != "ok" }
    /// The photo slot's scan line runs while a taken photo is read (v36 `scan`).
    private var scanning: Bool { shot != nil && (item.status == "looking" || item.status == "reading") }

    var body: some View {
        // "Source 2" sits on the source line; the row keeps Open source / Back
        let second = buttons.first { $0.args?["n"] == "2" }
        VStack(spacing: 36 * u) {
            HStack(alignment: .center, spacing: L.widePhotoPad * u) {
            // a photo question: the ⑦ photo slot at the left of the ⑧ card
            if item.isPhoto { photoSlot }
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 14 * u) {
                    Image(systemName: item.isPhoto ? "camera.fill" : pending ? "magnifyingglass" : "globe")
                        .foregroundStyle(Theme.polishBlue)
                    // 12-hour, as Milo says it (server search_label clock)
                    Text([item.day, item.clock ?? item.time].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(Theme.ui(30 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                    Text(source).font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
                }
                .font(Theme.ui(28 * u))
                .enter(.rise, 0, u: u)
                // the question shows at once, before any answer
                Text("“\(item.question)”").font(Theme.ui(26 * u)).foregroundStyle(Theme.text1)
                    .lineLimit(1).padding(.top, 26 * u).enter(.rise, 1, u: u)
                Group {
                    if pending {
                        loading
                    } else if failed {
                        Text(item.line ?? "").font(Theme.ui(40 * u, weight: .semibold))
                            .foregroundStyle(Theme.text1).lineLimit(2)
                            .padding(.top, 14 * u).enter(.text, 0, u: u)
                    } else {
                        answer(second)
                    }
                }
                .id(item.status)  // the answer replaces the loading card, then text comes in
            }
            }
            .padding(.horizontal, (item.isPhoto ? L.widePhotoPad : L.widePad) * u)
            .padding(.vertical, (item.isPhoto ? L.widePhotoPad : L.widePadV) * u)
            .frame(width: L.wideCard * u, alignment: .leading)
            .benchCard(radius: 44 * u)
            .modifier(Bump(value: item.status))
            SayRow(buttons: buttons.filter { $0 != second }, u: u, perform: perform).frame(width: L.wideCard * u)
        }
        .frame(maxHeight: .infinity)
        .onChange(of: commands?.gen) { _, _ in
            if commands?.last?.cmd == "take", item.status == "looking" { onTake() }
        }
    }

    /// Where the answer came from: the web, or read off the photo (no search).
    private var source: String {
        switch item.status {
        case "looking": "Show me"
        case "reading": "Reading the photo"
        case "pending": "Searching the web"
        default: item.isPhoto && item.basis == "photo" ? "Read from photo" : "Searched the web"
        }
    }

    /// ⑦'s photo slot (300, radius 32): the hint while waiting, the glasses'
    /// photo the moment it's taken (scan line while it's read), then the kept
    /// crop with SAM's outline.
    private var photoSlot: some View {
        ZStack {
            Theme.sub
            if item.status != "ok", let shot {
                // whole, like the live view: the photo's frame and the stream's differ, a fill crops them differently
                Image(uiImage: shot).resizable().scaledToFit()
            } else if item.status == "looking" {
                Group {
                    if let live {
                        Image(uiImage: live).resizable().scaledToFit()  // what the glasses see, whole
                    } else {
                        VStack(spacing: 14 * u) {
                            Image(systemName: "eyeglasses").font(Theme.ui(56 * u)).foregroundStyle(Theme.text4)
                            Text(Persona.line("look_hint")).font(Theme.ui(20 * u)).foregroundStyle(Theme.text2)
                                .multilineTextAlignment(.center).padding(.horizontal, 24 * u)
                        }
                    }
                }
                .onAppear { onLive(true) }
                .onDisappear { onLive(false) }
            } else if item.status == "ok" {
                AsyncImage(url: PipClient.lookCropURL(item.id)) { img in
                    img.resizable().scaledToFit()
                        .overlay { OutlineShape(points: item.outline ?? []).stroke(Theme.polishBlue, lineWidth: 3 * u) }
                } placeholder: { shotView }
            } else {
                shotView
            }
            if scanning { ScanLine(u: u) }
        }
        .frame(width: L.photoSlot * u, height: L.photoSlot * u)
        .clipShape(RoundedRectangle(cornerRadius: L.photoRadius * u, style: .continuous))
        .enter(.pop, 0, u: u)
    }

    @ViewBuilder private var shotView: some View {
        if let shot {
            Image(uiImage: shot).resizable().scaledToFill()
        } else {
            Image(systemName: "camera.fill").font(Theme.ui(69 * u)).foregroundStyle(Theme.text4)
        }
    }

    /// The text column's width: the card (1000) less its padding, and less
    /// the photo slot (300 + 48) on a photo question.
    private var textWidth: CGFloat {
        item.isPhoto ? L.wideCard - 3 * L.widePhotoPad - L.photoSlot : L.wideCard - 2 * L.widePad
    }

    /// Answer and detail placeholders with the HTML sheen running across;
    /// bar lengths are shares of the text column, so they never leave the card.
    private var loading: some View {
        let bars: [(CGFloat, CGFloat)] = [(620.0 / 872, 56), (760.0 / 872, 24), (520.0 / 872, 24)]
        return VStack(alignment: .leading, spacing: 18 * u) {
            ForEach(bars.indices, id: \.self) { i in
                Capsule().fill(Theme.wellEmpty).frame(width: bars[i].0 * textWidth * u, height: bars[i].1 * u)
            }
        }
        .padding(.top, 22 * u)
        .overlay { Sheen() }
        .mask { VStack(alignment: .leading, spacing: 18 * u) {
            ForEach(bars.indices, id: \.self) { i in
                Capsule().frame(width: bars[i].0 * textWidth * u, height: bars[i].1 * u)
            }
        }.padding(.top, 22 * u) }
        .accessibilityLabel(item.isPhoto ? "Waiting for the photo" : "Searching")
    }

    private func answer(_ second: ScreenButton?) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(item.answerShort).font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
                .padding(.top, 14 * u).enter(.text, 0, u: u)
            Text(item.detail).font(Theme.ui(28 * u)).foregroundStyle(Theme.text2)
                .lineLimit(2).minimumScaleFactor(0.7).padding(.top, 14 * u).enter(.rise, 1, u: u)
            HStack(spacing: 16 * u) {
                if let s = item.sources.first {
                    // what backs it, said plainly: a cited source, or only pages searched
                    (Text(item.cited ? "Source " : "Pages searched ").fontWeight(.semibold)
                     + Text("\(s.title) · \(s.domain)")
                     + Text(item.sources.count > 1 ? " +\(item.sources.count - 1)" : ""))
                        .lineLimit(1).foregroundStyle(Theme.text2)
                }
                if let second {
                    Button { perform(second) } label: {
                        (Say.prefix(Theme.polishBlue)
                         + Text(second.say).fontWeight(.semibold).foregroundStyle(Theme.text1))
                    }
                    .buttonStyle(.plain)
                }
            }
            .font(Theme.ui(20 * u)).padding(.top, 30 * u).enter(.rise, 2, u: u)
        }
    }
}

/// SAM's outline of the thing asked about, 0-1 points over the crop.
struct OutlineShape: Shape {
    var points: [[Double]]

    func path(in rect: CGRect) -> Path {
        var p = Path()
        let pts = points.filter { $0.count == 2 }.map {
            CGPoint(x: rect.minX + $0[0] * rect.width, y: rect.minY + $0[1] * rect.height)
        }
        guard let first = pts.first, pts.count > 2 else { return p }
        p.move(to: first)
        pts.dropFirst().forEach { p.addLine(to: $0) }
        p.closeSubpath()
        return p
    }
}

/// v36 `.photo::after` scan: a 90 px band (transparent -> blue .22 ->
/// transparent) moving top to bottom in 1.3 s ease-in-out, again and
/// again while the photo is awaited or read. Reduce Motion: none.
struct ScanLine: View {
    var u: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var down = false

    var body: some View {
        GeometryReader { g in
            if !reduce {
                LinearGradient(colors: [.clear, Theme.blue.opacity(0.22), .clear], startPoint: .top, endPoint: .bottom)
                    .frame(height: 90 * u)
                    .offset(y: down ? g.size.height : -90 * u)
                    .onAppear {
                        withAnimation(.easeInOut(duration: 1.3).delay(0.35).repeatForever(autoreverses: false)) {
                            down = true
                        }
                    }
            }
        }
        .allowsHitTesting(false)
    }
}

/// HTML `sheen`: a white band (100°, .35 at the middle) crossing left to
/// right every 1.4 s, ease-in-out. Reduce Motion: a soft pulse in place.
struct Sheen: View {
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var on = false

    var body: some View {
        GeometryReader { g in
            let band = LinearGradient(stops: [.init(color: .clear, location: 0.3),
                                              .init(color: .white.opacity(Theme.Motion.sheenPeak), location: 0.5),
                                              .init(color: .clear, location: 0.7)],
                                      startPoint: UnitPoint(x: 0, y: 0.41), endPoint: UnitPoint(x: 1, y: 0.59))
            if reduce {
                Color.white.opacity(on ? Theme.Motion.sheenPeak : 0)
                    .animation(Theme.Motion.ease(Theme.Motion.sheen).repeatForever(autoreverses: true), value: on)
            } else {
                band.frame(width: g.size.width)
                    .offset(x: on ? g.size.width : -g.size.width)
                    .animation(Theme.Motion.ease(Theme.Motion.sheen).repeatForever(autoreverses: false), value: on)
            }
        }
        .onAppear { on = true }
        .allowsHitTesting(false)
    }
}

// MARK: - ⓪c Web searches

struct WebSearchesScreen: View {
    var items: [WebSearch]
    var buttons: [ScreenButton]
    var u: CGFloat
    var onOpen: (String) -> Void
    var perform: (ScreenButton) -> Void

    /// Rows that fit the card without scrolling; older ones page.
    private let perPage = 4
    @State private var page = 0

    var body: some View {
        let pages = max(1, (items.count + perPage - 1) / perPage)
        let p = min(page, pages - 1)
        let shown = Array(items[(p * perPage)..<min(items.count, (p + 1) * perPage)])
        VStack(spacing: 36 * u) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(shown.enumerated()), id: \.element.id) { i, s in
                    if i == 0 || shown[i - 1].day != s.day {
                        Text(s.day.uppercased()).font(Theme.ui(20 * u, weight: .semibold)).tracking(1.2 * u)
                            .foregroundStyle(Theme.text2).padding(.top, i == 0 ? 0 : 24 * u).padding(.bottom, 8 * u)
                    }
                    row(s).enter(.rise, i, u: u)
                }
                if pages > 1 {
                    Button { withAnimation(Theme.Motion.ease(Theme.Motion.seg)) { page = (p + 1) % pages } } label: {
                        Text(p + 1 < pages ? "+\(items.count - (p + 1) * perPage) older" : "Back to the latest")
                            .font(Theme.ui(22 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                    }
                    .buttonStyle(.plain).padding(.top, 8 * u)
                }
                if let ex = items.first {
                    // the way to say one, from a real search
                    Rectangle().fill(Theme.cardOff).frame(height: 1).padding(.top, 18 * u)
                    (Text("Say").foregroundStyle(Theme.polishBlue)
                     + Text(" a date and time, like ").foregroundStyle(Theme.text2)
                     + Text(example(ex)).fontWeight(.semibold).foregroundStyle(Theme.text1))
                        .font(Theme.ui(22 * u)).padding(.top, 18 * u).padding(.leading, 14 * u)
                }
            }
            .padding(.horizontal, 56 * u).padding(.vertical, 44 * u)
            .frame(width: 1000 * u, alignment: .leading)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 620 * u)
        }
        .frame(maxHeight: .infinity)
    }

    private func example(_ s: WebSearch) -> String {
        let d = Date(timeIntervalSince1970: s.ts)
        return "\(d.formatted(.dateTime.month(.abbreviated).day())), \(s.time)"
    }

    private func row(_ s: WebSearch) -> some View {
        Button { onOpen(s.id) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                Text(s.time).font(Theme.ui(28 * u)).foregroundStyle(Theme.polishBlue)
                    .frame(width: 135 * u, alignment: .leading).padding(.leading, 14 * u)
                VStack(alignment: .leading, spacing: 4 * u) {
                    Text(s.question).font(Theme.ui(28 * u)).foregroundStyle(Theme.text1)
                    Text(s.answerShort).font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
                }
                .lineLimit(1)
                Spacer(minLength: 12 * u)
                Image(systemName: "chevron.right").font(Theme.ui(18 * u)).foregroundStyle(Theme.text4)
            }
            .padding(.vertical, 12 * u)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
