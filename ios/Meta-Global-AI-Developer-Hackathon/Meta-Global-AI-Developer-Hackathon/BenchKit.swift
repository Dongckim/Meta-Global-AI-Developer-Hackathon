import SwiftUI

/// Shared parts of the Figma "ver 2 - Final" screens (Glassistant file,
/// node 20:31108): light canvas, white cards, "Say X" buttons, the header
/// timer chip. Colors come only from `Theme`; sizes are design px at 1376
/// wide (`Theme.px`).

/// Gray canvas with the soft blue glow at the lower right.
struct BenchCanvas: View {
    var body: some View {
        GeometryReader { g in
            let px = { (v: CGFloat) in Theme.px(v, in: g.size.width) }
            Theme.canvas
                .overlay(alignment: .topLeading) {
                    Ellipse().fill(Theme.canvasGlow)
                        .frame(width: px(618), height: px(621))
                        .blur(radius: px(118))
                        .offset(x: px(860), y: px(640))
                }
                .clipped()
        }
        .ignoresSafeArea()
    }
}

extension View {
    /// White card: radius 32, three-layer soft shadow.
    func benchCard(radius: CGFloat = 32) -> some View {
        background {
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(Theme.card)
                .shadow(color: .black.opacity(0.04), radius: 1, y: 1)
                .shadow(color: .black.opacity(0.07), radius: 10, y: 8)
                .shadow(color: .black.opacity(0.12), radius: 28, y: 28)
        }
    }
}

/// "Say Continue": a registry button (screens.json `buttons`). The label is
/// the button's `say`, which is also a screen command and what the server
/// matches when spoken; a tap sends the same action through `perform`
/// (the owner's /input action path). No free-text label.
struct SayButton: View {
    enum Kind { case primary, secondary }
    var button: ScreenButton
    var kind: Kind = .primary
    var size: CGFloat = 34
    var perform: (ScreenButton) -> Void

    private var word: String { button.say }

    var body: some View {
        Button { perform(button) } label: {
            (Text("Say ").foregroundStyle(kind == .primary ? Color.white.opacity(0.72) : Theme.polishBlue)
             + Text(word).fontWeight(.semibold).foregroundStyle(kind == .primary ? .white : Theme.text1))
                .font(.system(size: size))
                .tracking(-0.54 * size / 34)
                .lineLimit(1)
                .frame(maxWidth: .infinity, minHeight: size * 93 / 34)
                .background {
                    Capsule().fill(kind == .primary ? Theme.polishBlue : Theme.card)
                        .shadow(color: .black.opacity(kind == .primary ? 0.08 : 0.05), radius: 1, y: 1)
                        .shadow(color: kind == .primary ? Theme.polishBlue.opacity(0.6) : .black.opacity(0.14),
                                radius: kind == .primary ? 14 : 10, y: kind == .primary ? 12 : 8)
                }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(word)
    }
}

/// Header chip: the soonest timer's wells, minutes left, and how many more
/// ("B2–B8 6 min +1"). Hidden with no running timer.
struct TimerChip: View {
    var timers: [TimerEntry]
    var size: CGFloat = 22

    struct Summary: Equatable {
        var wells: String
        var minutesLeft: Int
        /// Elapsed fraction 0...1 (the ring).
        var progress: Double
        var more: Int
    }

    /// Running timers only; the one ending first leads.
    static func summary(_ timers: [TimerEntry], now: Date) -> Summary? {
        let running = timers
            .map { ($0, $0.minutes * 60 - now.timeIntervalSince($0.started)) }
            .filter { $0.1 > 0 }
            .sorted { $0.1 < $1.1 }
        guard let (t, rem) = running.first else { return nil }
        return Summary(wells: t.wells.isEmpty ? t.label : t.wells,
                       minutesLeft: Int((rem / 60).rounded(.up)),
                       progress: t.minutes > 0 ? min(1, max(0, 1 - rem / (t.minutes * 60))) : 1,
                       more: running.count - 1)
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            if let s = Self.summary(timers, now: ctx.date) {
                HStack(spacing: size * 0.45) {
                    ZStack {
                        Circle().stroke(Theme.cardOff, lineWidth: size * 0.12)
                        Circle().trim(from: 0, to: s.progress)
                            .stroke(Theme.polishBlue, style: StrokeStyle(lineWidth: size * 0.12, lineCap: .round))
                            .rotationEffect(.degrees(-90))
                    }
                    .frame(width: size * 0.9, height: size * 0.9)
                    Text(s.wells).fontWeight(.semibold).foregroundStyle(Theme.text1)
                    Text("\(s.minutesLeft) min").foregroundStyle(Theme.text3)
                    if s.more > 0 {
                        Text("+\(s.more)").fontWeight(.medium).foregroundStyle(Theme.polishBlue)
                    }
                }
                .font(.system(size: size).monospacedDigit())
                .lineLimit(1)
                .padding(.horizontal, size * 0.55)
                .frame(height: size * 50 / 22)
                .background(Capsule().fill(.white.opacity(0.8))
                    .shadow(color: .black.opacity(0.04), radius: 1, y: 1))
                .accessibilityElement(children: .combine)
            }
        }
    }
}
