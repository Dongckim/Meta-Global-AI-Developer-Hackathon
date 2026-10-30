import os
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

/// Renders a `ScreenStack` in one ZStack: the front screen comes in and the
/// old one goes with one SwiftUI transition (opacity + a slight scale,
/// Theme.Motion.screen). The transition runs on the render layer, so no
/// screen body is rebuilt per frame, and one `show` is the only state change.
/// Nothing is drawn until the size is known (the first measure can be zero);
/// the floor is the front screen's color, edge to edge.
struct ScreenTransition<ID: Hashable, Screen: View>: View {
    @Binding var stack: ScreenStack<ID>
    var floor: (ID) -> Color
    @ViewBuilder var screen: (ID) -> Screen

    var body: some View {
        GeometryReader { g in
            ZStack {
                floor(stack.front).ignoresSafeArea()
                if g.size.width > 0, g.size.height > 0 {
                    screen(stack.front)
                        .id(stack.front)
                        .transition(.opacity.combined(with: .scale(scale: Theme.Motion.screenScale)))
                }
            }
            .animation(Theme.Motion.ease(Theme.Motion.screen), value: stack.front)
            .background { LayoutProbe(name: "root") }
        }
    }
}

/// [layout] lines for the first settles of a screen's geometry: size, safe
/// area insets and the 1376-wide canvas scale, each time one changes.
struct LayoutProbe: View {
    var name: String
    /// Status bar hidden as the root sets it (logged beside the insets).
    var statusBarHidden = true
    @State private var seen = 0

    var body: some View {
        GeometryReader { g in
            Color.clear
                .onAppear { log(g) }
                .onChange(of: g.size) { _, _ in log(g) }
                .onChange(of: g.safeAreaInsets) { _, _ in log(g) }
        }
    }

    private func log(_ g: GeometryProxy) {
        guard seen < 12 else { return }
        seen += 1
        let i = g.safeAreaInsets
        Log.layout.info("[layout] \(Boot.ms)ms \(name, privacy: .public) #\(seen) size=\(Int(g.size.width))x\(Int(g.size.height)) safe(t\(Int(i.top)) b\(Int(i.bottom)) l\(Int(i.leading)) r\(Int(i.trailing))) scale=\(String(format: "%.3f", g.size.width / Theme.canvasWidth), privacy: .public) statusBarHidden=\(statusBarHidden)")
    }
}
