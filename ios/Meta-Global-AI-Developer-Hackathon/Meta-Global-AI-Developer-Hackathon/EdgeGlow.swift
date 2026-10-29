import SwiftUI

/// Mic loudness follower: fast attack, slow release (exponential).
struct LevelFollower {
    private(set) var value: Float = 0
    var attack: Double = 0.05
    var release: Double = 0.4

    mutating func step(_ input: Float, dt: Double) -> Float {
        let tau = input > value ? attack : release
        let a = Float(1 - exp(-max(dt, 0) / tau))
        value += (input - value) * a
        return value
    }
}

/// Apple-Intelligence-style spectrum around the screen edge (design `.edge`):
/// the soft blurred band only (the crisp solid line was dropped on review),
/// conic colors spinning. One overlay
/// above every screen; reads VoiceSession state, the tap level and TTS word
/// timing only. Not drawn at all while ready (after the .5 s fade).
struct EdgeGlow: View {
    var voice: VoiceSession
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false
    /// Per-frame scratch (not observed): follower, last tick, last style for the fade-out.
    @State private var meter = Meter()

    final class Meter {
        var follower = LevelFollower()
        var lastTick: Date?
        var lastStyle: Theme.EdgeStyle?
    }

    private var active: Bool { voice.state != .ready }

    var body: some View {
        GeometryReader { g in
            if shown {
                TimelineView(.animation(minimumInterval: 1.0 / 30)) { ctx in
                    frame(at: ctx.date, width: g.size.width)
                }
            }
        }
        .opacity(active ? 1 : 0)
        .animation(.easeInOut(duration: Theme.edgeFade), value: active)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
        .onChange(of: active, initial: true) { _, on in
            if on {
                shown = true
            } else {
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(Theme.edgeFade))
                    if !active { shown = false }
                }
            }
        }
    }

    @ViewBuilder
    private func frame(at now: Date, width: CGFloat) -> some View {
        let dt = meter.lastTick.map { now.timeIntervalSince($0) } ?? 0
        let _ = meter.lastTick = now
        let level = meter.follower.step(MicEngine.shared.level, dt: dt)
        let pulse = Theme.wordPulse(since: SpeechBus.shared.lastWordAt, now: now)
        let live = Theme.edgeStyle(for: voice.state, level: level, pulse: pulse, reduceMotion: reduceMotion)
        let _ = live.map { meter.lastStyle = $0 }
        if let style = live ?? meter.lastStyle {
            let px = { (v: CGFloat) in Theme.px(v, in: width) }
            let angle = style.spin.map { now.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: $0) / $0 * 360 } ?? 0
            band(style, angle: angle, width: px(Theme.edgeSoftBand) * style.thickness, radius: px(Theme.edgeRadius))
                .blur(radius: px(Theme.edgeSoftBlur))
                .opacity(style.brightness)
                .drawingGroup()
        }
    }

    /// CSS conic 0deg points up; SwiftUI's angular 0 points right.
    private func band(_ s: Theme.EdgeStyle, angle: Double, width: CGFloat, radius: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: radius, style: .continuous)
            .strokeBorder(AngularGradient(colors: s.colors, center: .center,
                                          angle: .degrees(angle - 90)),
                          lineWidth: width)
    }
}
