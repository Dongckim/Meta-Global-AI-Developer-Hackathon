import SwiftUI

/// Top-level screen switching (design `show()`): one front screen takes
/// input; the previous one plays scrOut underneath, then unmounts.
struct ScreenStack<ID: Hashable>: Equatable {
    private(set) var front: ID
    /// Previous front while its scrOut runs.
    private(set) var leaving: ID?
    /// Bumps per switch; stale `finishLeave` calls are ignored.
    private(set) var generation = 0

    init(_ id: ID) { front = id }

    mutating func show(_ id: ID) {
        guard id != front else { return }
        leaving = front
        front = id
        generation += 1
    }

    /// The leaving screen's scrOut ended (design: when scrIn finishes).
    mutating func finishLeave(generation g: Int) {
        if g == generation { leaving = nil }
    }

    func acceptsInput(_ id: ID) -> Bool { id == front }
    func isMounted(_ id: ID) -> Bool { id == front || id == leaving }
}

/// Renders a `ScreenStack`: incoming scrIn .6s over outgoing scrOut .5s
/// (polish layer), floor in the outgoing screen's color. `prewarm` screens
/// stay mounted (hidden) so they are built before their first switch.
struct ScreenTransition<ID: Hashable, Screen: View>: View {
    @Binding var stack: ScreenStack<ID>
    /// Every screen, any order (z follows front/leaving).
    var screens: [ID]
    var prewarm: Set<ID> = []
    var floor: (ID) -> Color
    @ViewBuilder var screen: (ID) -> Screen

    var body: some View {
        GeometryReader { g in
            let unit = g.size.width / Theme.canvasWidth
            ZStack {
                floor(stack.leaving ?? stack.front).ignoresSafeArea()
                ForEach(screens, id: \.self) { id in
                    if stack.isMounted(id) || prewarm.contains(id) {
                        ScreenLayer(role: role(id), unit: unit,
                                    generation: stack.generation) { g in
                            stack.finishLeave(generation: g)
                        } content: {
                            screen(id)
                        }
                        .zIndex(id == stack.front ? 2 : id == stack.leaving ? 1 : 0)
                    }
                }
            }
        }
    }

    private func role(_ id: ID) -> ScreenLayerRole {
        id == stack.front ? .front : id == stack.leaving ? .leaving : .hidden
    }
}

enum ScreenLayerRole: Equatable { case front, leaving, hidden }

private struct ScreenLayer<Content: View>: View {
    var role: ScreenLayerRole
    /// Design px -> points (1376-wide canvas).
    var unit: CGFloat
    var generation: Int
    var onLeft: (Int) -> Void
    @ViewBuilder var content: Content
    /// Same clamped clock rule as the screens inside, so scrIn and the
    /// screen's own entrance start on the same frame and pause together.
    @State private var clock = FrameClock()
    /// Role whose animation is running and its clock start. A role change
    /// renders at progress 0 until the task records the new start, so the
    /// first frame never uses the previous role's timing.
    @State private var phase: (role: ScreenLayerRole, start: Double)?
    @State private var animating = true

    private struct Key: Equatable {
        var role: ScreenLayerRole
        var generation: Int
    }

    private static var inDuration: Double { 0.6 }
    private static var outDuration: Double { 0.5 }

    var body: some View {
        TimelineView(.animation(paused: !animating)) { ctx in
            let t = clock.tick(ctx.date)
            let p = phase.map { $0.role == role ? t - $0.start : 0 } ?? 0
            // scrIn: from opacity 0, translateY 26, scale 1.01
            // scrOut: to opacity 0, translateY -14, scale .98
            let inP = role == .front
                ? Keyframes.tween(p, duration: Self.inDuration) : 1
            let outP = role == .leaving
                ? Keyframes.tween(p, duration: Self.outDuration) : 0
            content
                .scaleEffect(1.01 - 0.01 * inP - 0.02 * outP)
                .offset(y: (26 * (1 - inP) - 14 * outP) * unit)
                .opacity(role == .hidden ? 0 : inP * (1 - outP))
        }
        .allowsHitTesting(role == .front)
        .accessibilityHidden(role != .front)
        .task(id: Key(role: role, generation: generation)) {
            guard role != .hidden else {
                phase = nil
                animating = false
                return
            }
            animating = true
            let start = clock.t
            phase = (role, start)
            // design: the old screen is dropped when scrIn (.6s) ends
            while clock.t - start < Self.inDuration {
                try? await Task.sleep(for: .milliseconds(16))
                if Task.isCancelled { return }
            }
            if role == .leaving {
                onLeft(generation)
            } else {
                animating = false
            }
        }
    }
}
