import AVFoundation
import MWDATCore
import SwiftUI

/// Blurred-blob aurora behind splash, sync, and the voice orb.
/// Whole-field motion follows the design keyframes (readyBreath, pulse,
/// think, sweep, syncFlow, splashBloom); blobs also drift on alternating
/// 7s/9s loops like the design's wobble.
struct AuroraView: View {
    enum Mode: Equatable {
        case flow // syncFlow 6s
        case breath // readyBreath 5s
        case pulse // pulse 1.6s
        case still(scale: CGFloat)
        case think // think 4s, opacity .7
        case voice(amp: Double) // scale(1.24 + amp * .16)
        case sweep // sweep 2.4s
        case bloomThenBreath // splashBloom 2.4s once, then breath
    }

    var palette: [Color] = Theme.auroraBlue
    var mode: Mode = .flow
    @State private var on = false

    var body: some View {
        GeometryReader { g in
            let w = g.size.width, h = g.size.height
            ZStack {
                ForEach(0 ..< 6) { i in
                    Ellipse()
                        .fill(palette[i % palette.count])
                        .frame(width: w * [0.75, 0.55, 0.6, 0.45, 0.5, 0.3][i],
                               height: h * [0.8, 0.6, 0.62, 0.5, 0.45, 0.35][i])
                        .position(x: w * [0.5, 0.18, 0.82, 0.35, 0.66, 0.5][i],
                                  y: h * [0.4, 0.72, 0.7, 0.62, 0.45, 0.55][i])
                        .blur(radius: max(18, w / 14))
                        .opacity(i == 5 ? 0.55 : 0.3)
                        .offset(x: on ? (i % 2 == 0 ? 9 : -9) : (i % 2 == 0 ? -9 : 9))
                        .animation(
                            .easeInOut(duration: i % 2 == 0 ? 7 : 9)
                                .repeatForever(autoreverses: true),
                            value: on
                        )
                }
            }
            .scaleEffect(scale)
            .rotationEffect(.degrees(rotation))
            .offset(x: xTravel * w / max(1, Theme.canvasWidth))
            .opacity(opacity * (mode == .bloomThenBreath ? (on ? 1 : 0) : 1))
            .animation(anim, value: on)
        }
        .onAppear { on = true }
    }

    private var scale: CGFloat {
        switch mode {
        case .flow: on ? 0.98 : 0.85
        case .breath: on ? 1.05 : 1
        case .pulse: on ? 1.2 : 1.1
        case let .still(s): s
        case .think: on ? 0.82 : 0.75
        case let .voice(amp): 1.24 + CGFloat(amp) * 0.16
        case .sweep: on ? 0.95 : 0.9
        case .bloomThenBreath: on ? 1 : 0.45
        }
    }

    private var rotation: Double {
        switch mode {
        case .think: on ? 5 : -5
        case .bloomThenBreath: on ? 0 : -8
        default: 0
        }
    }

    private var xTravel: CGFloat {
        switch mode {
        case .flow: on ? 60 : -60
        case .sweep: on ? 40 : -40
        default: 0
        }
    }

    private var opacity: Double {
        mode == .think ? 0.7 : 1
    }

    private var anim: Animation? {
        switch mode {
        case .flow:
            .easeInOut(duration: Theme.syncFlow).repeatForever(autoreverses: true)
        case .breath:
            .easeInOut(duration: Theme.readyBreath).repeatForever(autoreverses: true)
        case .pulse:
            .easeInOut(duration: Theme.pulse).repeatForever(autoreverses: true)
        case .still: nil
        case .think:
            .easeInOut(duration: Theme.think).repeatForever(autoreverses: true)
        case .voice: nil
        case .sweep:
            .easeInOut(duration: Theme.sweep).repeatForever(autoreverses: true)
        case .bloomThenBreath:
            Theme.ease(duration: Theme.splashBloom)
        }
    }
}

/// Pure sync-row derivation from live device state. No progress simulation:
/// every row renders the current value, or an explicit failure + reason.
enum SyncRows {
    struct Row: Equatable {
        var state: Theme.SyncRow
        /// Right-side value (.sval).
        var value: String
        /// Reason or hint under the title when failed.
        var hint: String?
    }

    static func app(registration: RegistrationState, error: String?) -> Row {
        if let error {
            return Row(state: .failed, value: "Error", hint: error)
        }
        switch registration {
        case .registered:
            return Row(state: .ok, value: "Registered", hint: nil)
        case .registering:
            return Row(state: .checking, value: "Waiting…",
                       hint: "Approve in the Meta AI app.")
        case .available:
            return Row(state: .failed, value: "Not registered",
                       hint: "Register to link the Meta AI app.")
        case .unavailable:
            return Row(state: .failed, value: "Unavailable",
                       hint: "DAT isn't available on this device.")
        @unknown default:
            return Row(state: .failed, value: "Unknown", hint: nil)
        }
    }

    static func glasses(status: GlassesDAT.LinkStatus, name: String?,
                        battery: Int?, error: String?) -> Row {
        if let error {
            return Row(state: .failed, value: "Error", hint: error)
        }
        switch status {
        case .connected:
            var v = name ?? "Connected"
            if let battery { v += " · \(battery)%" }
            return Row(state: .ok, value: v, hint: nil)
        case .found:
            return Row(state: .failed, value: name ?? "Found",
                       hint: "Found, not linked — wake the glasses.")
        case .registered:
            return Row(state: .failed, value: "Not found",
                       hint: "Pair in the Meta AI app first.")
        case .unregistered:
            return Row(state: .failed, value: "Not found",
                       hint: "Register the Meta AI app first.")
        case .error:
            return Row(state: .failed, value: "Error", hint: nil)
        }
    }

    static func audio(hfpName: String?) -> Row {
        if let hfpName {
            return Row(state: .ok, value: "HFP · \(hfpName)", hint: nil)
        }
        return Row(state: .failed, value: "iPad mic",
                   hint: "Pick glasses audio in Control Center.")
    }

    static func camera(_ permission: PermissionStatus?) -> Row {
        switch permission {
        case .granted:
            return Row(state: .ok, value: "Granted", hint: nil)
        case .denied:
            return Row(state: .failed, value: "Denied",
                       hint: "Enable camera in the Meta AI app.")
        case .none:
            return Row(state: .failed, value: "Not checked",
                       hint: "Check the DAT camera permission.")
        }
    }
}

/// Setup's "Glasses (DAT)" section, restyled as the design's Glasses sync.
/// Reads live state only (DAT owner, audio route, DAT camera permission);
/// Retry re-checks on-demand reads, Skip finishes setup like Done.
struct GlassesSyncSection: View {
    var dat: GlassesDAT?
    /// Bumped by voice "retry the glasses check" (server sync_retry).
    var refreshGen: Int = 0
    var onSkip: () -> Void
    @State private var hfpName: String?
    @State private var celebrated = false
    @State private var registerNudge = false

    private var appRow: SyncRows.Row {
        guard let dat else {
            return SyncRows.Row(state: .failed, value: "Unavailable", hint: nil)
        }
        return SyncRows.app(registration: dat.registrationState,
                            error: dat.lastError)
    }

    private var glassesRow: SyncRows.Row {
        guard let dat else {
            return SyncRows.Row(state: .failed, value: "Unavailable", hint: nil)
        }
        return SyncRows.glasses(status: dat.status, name: dat.deviceName,
                                battery: dat.batteryLevel,
                                error: dat.status == .error ? dat.lastError : nil)
    }

    private var audioRow: SyncRows.Row { SyncRows.audio(hfpName: hfpName) }

    private var cameraRow: SyncRows.Row {
        SyncRows.camera(dat?.cameraPermission)
    }

    private var allOk: Bool {
        [appRow, glassesRow, audioRow, cameraRow]
            .allSatisfy { $0.state == .ok }
    }

    private var isRegistering: Bool {
        dat?.registrationState == .registering
    }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 0) {
                header
                row(title: "Meta AI app", row: appRow) {
                    if let dat, dat.registrationState != .registered,
                       !dat.busy, dat.registrationState != .unavailable {
                        Button("Register") { dat.register() }
                            .buttonStyle(.borderedProminent).controlSize(.small)
                    }
                }
                row(title: "Ray-Ban Meta", row: glassesRow) {}
                row(title: "Glasses audio", row: audioRow) {}
                row(title: "Camera", row: cameraRow) {
                    if let dat, dat.cameraPermission != .granted {
                        Button(dat.cameraPermission == nil ? "Check" : "Enable") {
                            if dat.cameraPermission == nil {
                                dat.checkCameraPermission()
                            } else {
                                dat.askCameraRedirect()
                            }
                        }
                        .buttonStyle(.bordered).controlSize(.small)
                    }
                }
                footer
            }
            .padding(16)
            .background(Theme.chromeBg, in: RoundedRectangle(cornerRadius: 18))
            #if DEBUG
            mockCorner
            #endif
        }
        .onAppear { refreshAudio() }
        .onChange(of: refreshGen) { _, _ in refreshAll() }
        .onReceive(NotificationCenter.default.publisher(
            for: AVAudioSession.routeChangeNotification)) { _ in refreshAudio() }
        .onChange(of: allOk) { _, ok in celebrated = ok }
        .task(id: isRegistering) {
            registerNudge = false
            guard isRegistering else { return }
            try? await Task.sleep(for: .seconds(60))
            if isRegistering { registerNudge = true }
        }
        .alert("Continue in the Meta AI app?",
               isPresented: Binding(
                   get: { dat?.showCameraRedirectConfirm == true },
                   set: { dat?.setCameraRedirectConfirm($0) })) {
            Button("Continue") { dat?.requestCameraPermission() }
            Button("Cancel", role: .cancel) {}
        }
    }

    private var header: some View {
        ZStack(alignment: .leading) {
            AuroraView(palette: Theme.auroraBlue, mode: .flow)
                .frame(height: 84)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .opacity(0.55)
            HStack(spacing: 12) {
                ZStack {
                    if celebrated { CelebrationGlow() }
                    Image(systemName: "eyeglasses")
                        .font(.title)
                        .foregroundStyle(.white)
                        .frame(width: 52, height: 52)
                        .background(.white.opacity(0.12), in: Circle())
                }
                Text(allOk ? "Glasses connected" : "Connecting your glasses")
                    .font(.system(size: 24, weight: .medium))
                    .foregroundStyle(.white)
            }
            .padding(.horizontal, 12)
        }
        .padding(.bottom, 8)
    }

    private func row(title: String, row: SyncRows.Row,
                     @ViewBuilder action: () -> some View) -> some View {
        let style = Theme.syncStyle(for: row.state)
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                syncIcon(style.icon)
                Text(title)
                    .font(.system(size: 17))
                    .foregroundStyle(.white)
                Spacer()
                Text(row.value)
                    .font(.system(size: 15))
                    .foregroundStyle(.white.opacity(0.62))
                    .lineLimit(1)
                action()
            }
            .padding(.vertical, 12)
            if let hint = row.hint, row.state != .ok {
                Text(hint)
                    .font(.callout)
                    .foregroundStyle(Theme.chromeMut)
                    .padding(.leading, 30)
                    .padding(.bottom, 6)
            }
            if title == "Meta AI app", registerNudge, isRegistering {
                Text("Still waiting — approve in the Meta AI app, or Skip below.")
                    .font(.callout)
                    .foregroundStyle(Theme.chromeMut)
                    .padding(.leading, 30)
                    .padding(.bottom, 6)
            }
        }
        .opacity(style.rowOpacity == 1 ? 1 : 0.75)
        .overlay(alignment: .bottom) {
            Rectangle().fill(.white.opacity(0.1)).frame(height: 1)
        }
    }

    @ViewBuilder
    private func syncIcon(_ icon: Theme.SyncStyle.SyncIcon) -> some View {
        switch icon {
        case .spin:
            Circle()
                .trim(from: 0, to: 0.75)
                .stroke(.white, lineWidth: 2.5)
                .frame(width: 18, height: 18)
                .rotationEffect(.degrees(spin ? 360 : 0))
                .animation(.linear(duration: Theme.spin)
                    .repeatForever(autoreverses: false), value: spin)
                .onAppear { spin = true }
        case .tick:
            ZStack {
                Circle().fill(.white).frame(width: 18, height: 18)
                Image(systemName: "checkmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Theme.blue)
            }
        case .hollow:
            Circle()
                .stroke(.white.opacity(0.35), lineWidth: 2)
                .frame(width: 18, height: 18)
        }
    }

    @State private var spin = false

    private var footer: some View {
        HStack(spacing: 12) {
            if !allOk {
                Button("Retry") { refreshAll() }
                .buttonStyle(.bordered).controlSize(.small)
                .tint(.white)
                Button("Skip — use iPad mic") { onSkip() }
                    .font(.callout)
                    .foregroundStyle(Theme.chromeMut)
            }
            Spacer()
            if let dat, dat.registrationState == .registered {
                Button("Unregister") { dat.unregister() }
                    .font(.callout)
                    .foregroundStyle(Theme.chromeMut)
            }
        }
        .padding(.top, 12)
    }

    #if DEBUG
    private var mockCorner: some View {
        HStack(spacing: 6) {
            Button((dat?.mockEnabled ?? false) ? "Mock on" : "Mock") {
                if let dat { dat.setMockEnabled(!dat.mockEnabled) }
            }
            .font(.caption2)
            Button("Pair") { dat?.pairMockGlasses() }
                .font(.caption2)
                .disabled(dat?.mockEnabled != true)
            Button("Power") { dat?.powerOnMockDevices() }
                .font(.caption2)
                .disabled(dat?.mockEnabled != true)
        }
        .buttonStyle(.bordered)
        .controlSize(.mini)
        .tint(.white)
        .padding(8)
    }
    #endif

    /// Same re-check for the Retry button and voice "retry".
    private func refreshAll() {
        dat?.clearError()
        dat?.checkCameraPermission()
        refreshAudio()
    }

    private func refreshAudio() {
        let inputs = AVAudioSession.sharedInstance().currentRoute.inputs
        hfpName = inputs.first {
            $0.portType == .bluetoothHFP
        }?.portName
    }
}

/// One-shot readyGlow (1.2s): ring blooms behind the header icon.
private struct CelebrationGlow: View {
    @State private var t = 0.0

    var body: some View {
        Circle()
            .stroke(Theme.blue, lineWidth: 3)
            .frame(width: 52, height: 52)
            .scaleEffect(0.95 + 0.3 * t)
            .opacity((1 - t) * 0.9)
            .onAppear {
                withAnimation(Theme.ease(duration: Theme.readyGlow)) { t = 1 }
            }
    }
}
