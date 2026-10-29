import SwiftUI

/// CSS keyframe math for the dark screens. Every animated value is a pure
/// function of seconds since the screen appeared, so screens render the
/// HTML timeline frame by frame (and DEBUG captures can freeze it).
nonisolated enum Keyframes {
    /// CSS cubic-bezier(x1, y1, x2, y2) evaluated at progress `x`.
    static func bezier(_ x: Double, _ x1: Double, _ y1: Double,
                       _ x2: Double, _ y2: Double) -> Double {
        if x <= 0 { return 0 }
        if x >= 1 { return 1 }
        func coord(_ t: Double, _ a: Double, _ b: Double) -> Double {
            3 * (1 - t) * (1 - t) * t * a + 3 * (1 - t) * t * t * b + t * t * t
        }
        var lo = 0.0, hi = 1.0
        for _ in 0 ..< 30 {
            let mid = (lo + hi) / 2
            if coord(mid, x1, x2) < x { lo = mid } else { hi = mid }
        }
        return coord((lo + hi) / 2, y1, y2)
    }

    /// --ease cubic-bezier(.2,.8,.2,1).
    static func ease(_ x: Double) -> Double { bezier(x, 0.2, 0.8, 0.2, 1) }
    /// CSS ease-in-out.
    static func easeInOut(_ x: Double) -> Double { bezier(x, 0.42, 0, 0.58, 1) }
    /// --spring cubic-bezier(.34,1.56,.64,1) (overshoots past 1).
    static func spring(_ x: Double) -> Double { bezier(x, 0.34, 1.56, 0.64, 1) }

    /// One-shot animation with fill-mode both: 0 before `delay`, 1 after.
    static func tween(_ t: Double, delay: Double = 0, duration: Double,
                      curve: (Double) -> Double = ease) -> Double {
        curve((t - delay) / duration)
    }

    /// Infinite ease-in-out keyframes `0%,100% {A} 50% {B}`: 0 at A, 1 at B.
    /// Symmetric, so `animation-direction: reverse` renders the same.
    static func pingPong(_ t: Double, period: Double) -> Double {
        let phase = (t.truncatingRemainder(dividingBy: period) + period)
            .truncatingRemainder(dividingBy: period) / period
        return easeInOut(phase < 0.5 ? phase * 2 : (1 - phase) * 2)
    }
}

/// Screen clock: starts at the first drawn frame and advances at most
/// 1/30s per frame, so a stalled frame (launch work, first blur) pauses
/// the motion instead of skipping ahead. DEBUG `-uiPreviewTime <s>`
/// freezes it.
final class FrameClock {
    private var last: Date?
    private(set) var t = 0.0

    /// Call with each TimelineView date; same date twice adds nothing.
    func tick(_ now: Date) -> Double {
        #if DEBUG
        if let frozen = UserDefaults.standard.string(forKey: "uiPreviewTime")
            .flatMap(Double.init) {
            t = frozen
            return t
        }
        #endif
        if let last {
            t += min(max(now.timeIntervalSince(last), 0), 1.0 / 30)
        }
        last = now
        return t
    }
}

/// `.scr.dark`: --dark fill + inset 0 0 68px rgba(255,255,255,.1), with
/// `content` laid out on the 1376x1032 design canvas and scaled to fit.
struct DarkCanvas<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        GeometryReader { g in
            let s = min(g.size.width / Theme.canvasWidth,
                        g.size.height / Theme.canvasHeight)
            ZStack {
                Theme.dark
                // Inset shadow: a band just outside the edge, blurred inward.
                Rectangle()
                    .stroke(.white.opacity(0.1), lineWidth: 136 * s)
                    .padding(-68 * s)
                    .blur(radius: 34 * s)
                content
                    .frame(width: Theme.canvasWidth, height: Theme.canvasHeight)
                    .scaleEffect(s)
                    .frame(width: g.size.width, height: g.size.height)
            }
        }
        .clipped()
        .ignoresSafeArea()
    }
}

/// Blue aurora (design JS `BLOBS` x `BLUE` palette) inside `.bloom`.
struct AuroraView: View {
    enum Mode: Equatable {
        /// splashBloom 2.4s, then readyBreath 5s.
        case splash
        /// syncFlow 6s.
        case sync
        /// readyGlow 1.2s starting at `since` seconds.
        case syncReady(since: Double)
    }

    var mode: Mode
    /// Seconds since the screen appeared.
    var t: Double

    /// [w, h, left, top, color, opacity, blur] from BLOBS, colors mapped
    /// through BLUE.
    private static let blobs: [(CGFloat, CGFloat, CGFloat, CGFloat, UInt32, Double, CGFloat)] = [
        (1288, 743, 659, 165, 0x525252, 0.30, 118),
        (1030, 599, -167, 497, 0x5AC8FA, 0.10, 94),
        (1028, 524, -170, 567, 0x5AC8FA, 0.10, 94),
        (1028, 524, 1480, 595, 0x5AC8FA, 0.10, 94),
        (1030, 599, 1505, 483, 0x5AC8FA, 0.10, 94),
        (543, 606, 266, 674, 0x2E8BFF, 0.20, 94),
        (288, 789, 452, 200, 0x4DA3FF, 0.20, 94),
        (695, 263, 338, 262, 0x0071E3, 1, 94),
        (497, 278, 849, 373, 0x4DA3FF, 1, 94),
        (318, 130, 520, 438, 0x3B6FA8, 1, 94),
        (571, 329, 570, 262, 0x7FD3FF, 0.20, 46),
        (335, 193, 582, 403, 0x7CC4FF, 0.90, 46),
        (129, 75, 670, 486, 0x5AA8F0, 1, 46),
    ]

    var body: some View {
        let bloom = Keyframes.tween(t, duration: 1)
        ZStack(alignment: .topLeading) {
            ForEach(Self.blobs.indices, id: \.self) { i in
                let b = Self.blobs[i]
                // wobble: 7s odd / 9s even children, 50% {translate(24,-18) scale(1.06)}
                let k = Keyframes.pingPong(t, period: i % 2 == 0 ? 7 : 9)
                Ellipse()
                    .fill(Color(hex: b.4))
                    .frame(width: b.0, height: b.1)
                    .scaleEffect(1 + 0.06 * k)
                    .offset(x: 24 * k, y: -18 * k)
                    .opacity(b.5)
                    .blur(radius: b.6)
                    .offset(x: b.2, y: b.3)
            }
        }
        .frame(width: Theme.canvasWidth, height: Theme.canvasHeight,
               alignment: .topLeading)
        .rotationEffect(.degrees(motion.rotation))
        .scaleEffect(motion.scale)
        .offset(x: motion.x)
        .opacity(motion.opacity)
        // .scr.dark.play .bloom: 1s from scale(.55), opacity 0
        .scaleEffect(0.55 + 0.45 * bloom)
        .opacity(bloom)
    }

    private var motion: (scale: CGFloat, rotation: Double, x: CGFloat, opacity: Double) {
        switch mode {
        case .splash:
            if t < Theme.splashBloom {
                let p = Keyframes.tween(t, duration: Theme.splashBloom)
                return (0.45 + 0.55 * p, -8 * (1 - p), 0, p)
            }
            let k = Keyframes.pingPong(t - Theme.splashBloom, period: Theme.readyBreath)
            return (1 + 0.05 * k, 0, 0, 1)
        case .sync:
            let k = Keyframes.pingPong(t, period: Theme.syncFlow)
            return (0.85 + 0.13 * k, 0, -60 + 120 * k, 1)
        case let .syncReady(since):
            let p = Keyframes.tween(t - since, duration: Theme.readyGlow)
            return (0.95 + 0.3 * p, 0, 0, 1)
        }
    }
}

/// `#i-glasses` symbol (viewBox 68x30, stroke 2.4, round caps/joins).
struct GlassesIcon: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        for dx in [0.0, 36.0] {
            // M4 7h24l-2 13a6 6 0 0 1-6 5h-8a6 6 0 0 1-6-5Z
            p.move(to: CGPoint(x: 4 + dx, y: 7))
            p.addLine(to: CGPoint(x: 28 + dx, y: 7))
            p.addLine(to: CGPoint(x: 26 + dx, y: 20))
            p.addRelativeArc(center: CGPoint(x: 20.085 + dx, y: 19), radius: 6,
                             startAngle: .degrees(9.6), delta: .degrees(81.2))
            p.addLine(to: CGPoint(x: 12 + dx, y: 25))
            p.addRelativeArc(center: CGPoint(x: 11.915 + dx, y: 19), radius: 6,
                             startAngle: .degrees(89.2), delta: .degrees(81.2))
            p.closeSubpath()
        }
        // M28 10c2-2.4 10-2.4 12 0
        p.move(to: CGPoint(x: 28, y: 10))
        p.addCurve(to: CGPoint(x: 40, y: 10), control1: CGPoint(x: 30, y: 7.6),
                   control2: CGPoint(x: 38, y: 7.6))
        // M4 7 1 4M64 7l3-3
        p.move(to: CGPoint(x: 4, y: 7))
        p.addLine(to: CGPoint(x: 1, y: 4))
        p.move(to: CGPoint(x: 64, y: 7))
        p.addLine(to: CGPoint(x: 67, y: 4))
        return p.applying(CGAffineTransform(scaleX: rect.width / 68,
                                            y: rect.height / 30))
    }
}

extension View {
    /// textIn .75s (polish: from opacity 0, translateY 12, blur 6), fill
    /// backwards, starting `delay` seconds after the screen appeared.
    func textIn(_ t: Double, delay: Double) -> some View {
        let p = Keyframes.tween(t, delay: delay, duration: 0.75)
        return opacity(p).offset(y: 12 * (1 - p)).blur(radius: 6 * (1 - p))
    }

    /// pop .7s spring (polish: from opacity 0, translateY 10, scale .95).
    func popIn(_ t: Double, delay: Double) -> some View {
        let p = Keyframes.tween(t, delay: delay, duration: 0.7,
                                curve: Keyframes.spring)
        return opacity(min(1, max(0, p)))
            .offset(y: 10 * (1 - p))
            .scaleEffect(0.95 + 0.05 * p)
    }
}
