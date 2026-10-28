import SwiftUI

/// Launch splash (design `data-id="splash"`): dark canvas, blue aurora
/// bloom, Glassistant brandmark. Self-dismisses after the bloom; tap skips.
struct SplashView: View {
    var onDone: () -> Void

    var body: some View {
        GeometryReader { g in
            ZStack {
                Theme.chromeBg.ignoresSafeArea()
                AuroraView(palette: Theme.auroraBlue, mode: .bloomThenBreath)
                    .ignoresSafeArea()
                Text("Glassistant")
                    .font(.system(
                        size: Theme.px(96, in: g.size.width), weight: .semibold))
                    .foregroundStyle(.white)
                    .shadow(color: Color(hex: 0x003C8C).opacity(0.35),
                            radius: 60, y: 6)
                    .brandIn()
            }
            .contentShape(Rectangle())
            .onTapGesture { onDone() }
            .task {
                try? await Task.sleep(for: .seconds(Theme.splashBloom + 0.4))
                onDone()
            }
        }
    }
}

private extension View {
    /// brandIn 1.4s ease, .25s delay, from below at 0 opacity.
    func brandIn() -> some View {
        modifier(BrandIn())
    }
}

private struct BrandIn: ViewModifier {
    @State private var on = false

    func body(content: Content) -> some View {
        content
            .opacity(on ? 1 : 0)
            .offset(y: on ? 0 : 24)
            .onAppear {
                withAnimation(Theme.ease(duration: 1.4).delay(0.25)) { on = true }
            }
    }
}
