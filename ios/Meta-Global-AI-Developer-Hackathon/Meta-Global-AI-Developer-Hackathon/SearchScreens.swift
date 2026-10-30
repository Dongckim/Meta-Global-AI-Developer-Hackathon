import SwiftUI

// Web search, ver 2: ⑧ Web search answer (Figma 37:14048) and ⓪c Home ·
// Web searches (37:13104). Data: GET /searches (server/search.py keeps every
// search sent; the list shows the answered ones with their cited sources).
// Buttons are the registry's (web_answer: Open source / Source 2 / Back;
// web_searches: Back); a row tap opens that search like saying it.

// MARK: - ⑧ Web search answer

struct WebAnswerScreen: View {
    var item: WebSearch
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    /// pending: opened when the search started; the answer fills in on done.
    private var pending: Bool { item.status == "pending" }
    private var failed: Bool { !pending && item.status != "ok" }

    var body: some View {
        // "Source 2" sits on the source line; the row keeps Open source / Back
        let second = buttons.first { $0.args?["n"] == "2" }
        VStack(spacing: 36 * u) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 14 * u) {
                    Image(systemName: pending ? "magnifyingglass" : "globe").foregroundStyle(Theme.polishBlue)
                    Text([item.day, item.time].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(Theme.ui(30 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
                    Text(pending ? "Searching the web" : "Searched the web")
                        .font(Theme.ui(22 * u)).foregroundStyle(Theme.text2)
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
            .padding(.horizontal, 64 * u).padding(.vertical, 60 * u)
            .frame(width: 1000 * u, alignment: .leading)
            .benchCard(radius: 44 * u)
            .modifier(Bump(value: item.status))
            SayRow(buttons: buttons.filter { $0 != second }, u: u, perform: perform).frame(width: 1000 * u)
        }
        .frame(maxHeight: .infinity)
    }

    /// Answer and detail placeholders with the HTML sheen running across.
    private var loading: some View {
        VStack(alignment: .leading, spacing: 18 * u) {
            Capsule().fill(Theme.wellEmpty).frame(width: 620 * u, height: 56 * u)
            Capsule().fill(Theme.wellEmpty).frame(width: 760 * u, height: 24 * u)
            Capsule().fill(Theme.wellEmpty).frame(width: 520 * u, height: 24 * u)
        }
        .padding(.top, 22 * u)
        .overlay { Sheen() }
        .mask { VStack(alignment: .leading, spacing: 18 * u) {
            Capsule().frame(width: 620 * u, height: 56 * u)
            Capsule().frame(width: 760 * u, height: 24 * u)
            Capsule().frame(width: 520 * u, height: 24 * u)
        }.padding(.top, 22 * u) }
        .accessibilityLabel("Searching")
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
