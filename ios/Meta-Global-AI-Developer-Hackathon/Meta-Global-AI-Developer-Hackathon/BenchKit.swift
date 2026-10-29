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
                    TimerRing(progress: s.progress, size: size * 0.9)
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
                .background(Capsule().fill(Theme.chip)
                    .shadow(color: .black.opacity(0.04), radius: 1, y: 1))
                .accessibilityElement(children: .combine)
            }
        }
    }
}

/// Elapsed-time ring of a timer (chip and timer rows).
struct TimerRing: View {
    /// Elapsed fraction 0...1.
    var progress: Double
    var size: CGFloat

    var body: some View {
        ZStack {
            Circle().stroke(Theme.cardOff, lineWidth: size * 0.13)
            Circle().trim(from: 0, to: progress)
                .stroke(Theme.polishBlue, style: StrokeStyle(lineWidth: size * 0.13, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .frame(width: size, height: size)
    }
}

// MARK: - Motion (values: Theme.Motion)

/// One entrance from the prototype's [data-a] set, played on appear; give
/// the view a new `.id` to replay it when its data changes. Reduce Motion:
/// opacity only.
struct Enter: ViewModifier {
    enum Kind { case rise, text, pop, left, right }
    var kind: Kind
    /// Stagger slot (0, 1, 2, ...).
    var order: Int = 0
    /// Design px -> points.
    var u: CGFloat
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        let M = Theme.Motion.self
        let off = shown || reduce
        content
            .opacity(shown ? 1 : 0)
            .offset(x: off ? 0 : (kind == .left ? -M.sideX : kind == .right ? M.sideX : 0) * u,
                    y: off ? 0 : (kind == .rise ? M.riseY : kind == .text ? M.textY : kind == .pop ? M.popY : 0) * u)
            .scaleEffect(off || kind != .pop ? 1 : M.popScale)
            .blur(radius: off || kind != .text ? 0 : M.textBlur * u)
            .onAppear {
                withAnimation(M.ease(M.enter).delay(M.enterBase + Double(order) * M.enterStep)) { shown = true }
            }
    }
}

/// cmdIn for the primary command: it rises in with a spring. Nothing draws
/// outside the button (no cue ring).
struct CommandIn: ViewModifier {
    var u: CGFloat
    var delay: Double = 0
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        let M = Theme.Motion.self
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduce ? 0 : M.cmdInY * u)
            .scaleEffect(shown || reduce ? 1 : M.cmdInScale)
            .onAppear { withAnimation(M.spring(M.cmdIn).delay(delay)) { shown = true } }
    }
}

/// shake-once: a short no-shake for something that needs a second look.
struct ShakeOnce: ViewModifier {
    var u: CGFloat
    @State private var fired = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        let M = Theme.Motion.self
        content.keyframeAnimator(initialValue: CGFloat(0), trigger: fired) { view, x in
            view.offset(x: x * u)
        } keyframes: { _ in
            KeyframeTrack {
                CubicKeyframe(0, duration: M.shakeDelay)
                for x in M.shakeX.dropFirst() {
                    CubicKeyframe(reduce ? 0 : x, duration: M.shake / Double(M.shakeX.count - 1))
                }
            }
        }
        .onAppear { fired = true }
    }
}

/// bump: a value that just changed settles in.
struct Bump<V: Equatable>: ViewModifier {
    var value: V
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        let M = Theme.Motion.self
        content.keyframeAnimator(initialValue: 1.0, trigger: value) { view, k in
            view.scaleEffect(reduce ? 1 : M.bumpScale + (1 - M.bumpScale) * k)
                .opacity(0.55 + 0.45 * k)
        } keyframes: { _ in
            KeyframeTrack {
                CubicKeyframe(0, duration: 0.001)
                SpringKeyframe(1, duration: M.bump)
            }
        }
    }
}

extension View {
    func enter(_ kind: Enter.Kind, _ order: Int = 0, u: CGFloat) -> some View {
        modifier(Enter(kind: kind, order: order, u: u))
    }
}

/// One bar per step: done in ink (each grows in, seg), `current` in blue,
/// the rest in `rest`. Give it a new `.id` to replay when the step moves.
struct StepBars: View {
    var count: Int
    var done: Int
    var current: Int
    var rest: Color = Theme.card
    /// Design px -> points.
    var u: CGFloat
    @State private var grown = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let M = Theme.Motion.self
        HStack(spacing: 8 * u) {
            ForEach(0..<max(count, 0), id: \.self) { i in
                Capsule().fill(rest)
                    .overlay(alignment: .leading) {
                        if i < done || i == current {
                            Capsule().fill(i == current ? Theme.polishBlue : Theme.text1)
                                .scaleEffect(x: grown || reduce || i == current ? 1 : 0, anchor: .leading)
                                .opacity(grown || !reduce ? 1 : 0)
                                .animation(M.ease(M.seg).delay(M.segBase + Double(i) * M.segStep), value: grown)
                        }
                    }
                    .frame(height: 6 * u)
            }
        }
        .onAppear { grown = true }
    }
}
