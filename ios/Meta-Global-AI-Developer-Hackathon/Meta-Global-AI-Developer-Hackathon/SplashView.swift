import SwiftUI

/// Launch splash (design `data-id="splash"`): dark canvas, blue aurora
/// bloom, Glassistant brandmark. Self-dismisses after 2.6s (design
/// `runSplash`); tap skips.
struct SplashView: View {
    var onDone: () -> Void
    @State private var clock = FrameClock()

    var body: some View {
        TimelineView(.animation) { ctx in
            let t = clock.tick(ctx.date)
            // brandIn 1.4s --ease .25s both: from opacity 0,
            // letter-spacing .06em, blur 14
            let p = Keyframes.tween(t, delay: 0.25, duration: 1.4)
            DarkCanvas {
                ZStack {
                    AuroraView(mode: .splash, t: t)
                    Text("Glassistant")
                        .font(.system(size: 96, weight: .semibold))
                        .tracking((0.06 + (-0.035 - 0.06) * p) * 96)
                        .foregroundStyle(.white)
                        .frame(height: 96)
                        .shadow(color: Color(hex: 0x003C8C).opacity(0.35),
                                radius: 30, y: 6)
                        .blur(radius: 14 * (1 - p))
                        .opacity(p)
                }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { onDone() }
        .task {
            // on the screen clock, so a launch stall doesn't cut the bloom
            while clock.t < 2.6 {
                try? await Task.sleep(for: .milliseconds(50))
                if Task.isCancelled { return }
            }
            onDone()
        }
    }
}
