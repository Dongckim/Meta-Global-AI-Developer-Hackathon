import AVFoundation
import MWDATCore
import SwiftUI

/// Pure sync-row derivation from live device state. No progress simulation:
/// every row renders the current value, or a failure with a short reason
/// as the value. `hint` is the longer fix, read by VoiceOver.
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
            return Row(state: .ok, value: "Linked", hint: nil)
        case .registering:
            return Row(state: .checking, value: "Linking…",
                       hint: "Approve in the Meta AI app.")
        case .available:
            return Row(state: .failed, value: "Not linked",
                       hint: "Register to link the Meta AI app.")
        case .unavailable:
            return Row(state: .failed, value: "Unavailable",
                       hint: "DAT unavailable — install the Meta AI app, then Retry.")
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
            let v = battery.map { "Found · \($0)%" } ?? "Found"
            return Row(state: .ok, value: v, hint: name)
        case .found:
            return Row(state: .failed, value: "Not connected",
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
            return Row(state: .ok, value: "Glasses mic", hint: hfpName)
        }
        return Row(state: .failed, value: "iPad mic",
                   hint: "Pick glasses audio in Control Center.")
    }

    static func camera(_ permission: PermissionStatus?) -> Row {
        switch permission {
        case .granted:
            return Row(state: .ok, value: "Allowed", hint: nil)
        case .denied:
            return Row(state: .failed, value: "Denied",
                       hint: "Enable camera in the Meta AI app.")
        case .none:
            return Row(state: .failed, value: "Not checked",
                       hint: "Check the DAT camera permission.")
        }
    }
}

/// Headless sync evaluation for the launch gate: the same rows the
/// screen renders. The gate shows unless all rows are ok or skipped.
enum SyncGate {
    static func rows(dat: GlassesDAT?, hfpName: String?) -> [SyncRows.Row] {
        let app: SyncRows.Row
        let glasses: SyncRows.Row
        if let dat {
            app = SyncRows.app(registration: dat.registrationState,
                               error: dat.lastError)
            glasses = SyncRows.glasses(
                status: dat.status, name: dat.deviceName,
                battery: dat.batteryLevel,
                error: dat.status == .error ? dat.lastError : nil)
        } else {
            app = SyncRows.Row(state: .failed, value: "Unavailable", hint: nil)
            glasses = SyncRows.Row(state: .failed, value: "Unavailable",
                                   hint: nil)
        }
        return [app, glasses, SyncRows.audio(hfpName: hfpName),
                SyncRows.camera(dat?.cameraPermission)]
    }

    static var liveHFPName: String? {
        AVAudioSession.sharedInstance().currentRoute.inputs.first {
            $0.portType == .bluetoothHFP
        }?.portName
    }

    static func needed(rows: [SyncRows.Row], skipped: Bool) -> Bool {
        !skipped && !rows.allSatisfy { $0.state == .ok }
    }
}


/// Fullscreen Glasses sync step (design `data-id="sync"`): live device
/// rows on the dark aurora canvas. Auto-advances shortly after all rows
/// turn ok ("Glasses ready"). Failed rows are tappable where a fix
/// exists (register, camera); Retry/Skip show only while a row failed.
struct GlassesSyncScreen: View {
    var dat: GlassesDAT?
    /// Bumped by voice "retry the glasses check" (server sync_retry).
    var refreshGen: Int = 0
    var onSkip: () -> Void
    var onComplete: () -> Void
    #if DEBUG
    /// Fixed rows for design captures (`-uiPreview connecting|ready`).
    var previewRows: [SyncRows.Row]?
    #endif
    @State private var hfpName: String?
    @State private var registerNudge = false
    @State private var start = Date()
    /// Clock time when every row turned ok (drives readyGlow / bump).
    @State private var readyAt: Double?

    /// Display order: design rows 1-3, then "Live video" -> glasses audio.
    private static let titles = ["Meta AI app", "Ray-Ban Meta", "Camera", "Glasses audio"]

    private var rows: [SyncRows.Row] {
        #if DEBUG
        if let previewRows { return previewRows }
        #endif
        let r = SyncGate.rows(dat: dat, hfpName: hfpName)
        return [r[0], r[1], r[3], r[2]]
    }

    private var allOk: Bool {
        rows.allSatisfy { $0.state == .ok }
    }

    private var isRegistering: Bool {
        dat?.registrationState == .registering
    }

    private var canRegister: Bool {
        guard let dat else { return false }
        return dat.registrationState != .registered && !dat.busy
            && dat.registrationState != .unavailable
    }

    var body: some View {
        TimelineView(.animation) { ctx in
            let t = Keyframes.elapsed(ctx.date, since: start)
            DarkCanvas {
                ZStack {
                    AuroraView(mode: readyAt.map { .syncReady(since: $0) } ?? .sync, t: t)
                    syncBlock(t)
                    if rows.contains(where: { $0.state == .failed }) {
                        footer
                            .frame(maxHeight: .infinity, alignment: .bottom)
                            .padding(.bottom, 44)
                    }
                }
            }
        }
        .overlay(alignment: .topTrailing) {
            #if DEBUG
            if previewRows == nil { mockCorner }
            #endif
        }
        .onAppear {
            if allOk { readyAt = 0 }
            refreshAudio()
            dat?.checkCameraPermission()
        }
        .onChange(of: refreshGen) { _, _ in refreshAll() }
        .onReceive(NotificationCenter.default.publisher(
            for: AVAudioSession.routeChangeNotification)) { _ in refreshAudio() }
        .onChange(of: allOk) { _, ok in
            readyAt = ok ? Date().timeIntervalSince(start) : nil
        }
        .task(id: allOk) {
            guard allOk else { return }
            do {
                try await Task.sleep(for: .seconds(1.6))
            } catch {
                return
            }
            onComplete()
        }
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

    /// `.sync`: 640 wide, gap 26; entrance delays follow the design's
    /// prepare(): 14 candidates, 55ms apart from 70ms.
    private func syncBlock(_ t: Double) -> some View {
        let sinceReady = readyAt.map { t - $0 }
        // .glasses scale .6s spring to 1.12; title bump .55s spring
        let grow = sinceReady.map {
            Keyframes.tween($0, duration: 0.6, curve: Keyframes.spring)
        } ?? 0
        let bump = sinceReady.map {
            Keyframes.tween($0, duration: 0.55, curve: Keyframes.spring)
        } ?? 1
        return VStack(spacing: 26) {
            GlassesIcon()
                .stroke(.white, style: StrokeStyle(lineWidth: 4.8, lineCap: .round,
                                                   lineJoin: .round))
                .frame(width: 136, height: 60)
                .scaleEffect(1 + 0.12 * grow)
                .popIn(t, delay: 0.07)
            Text(allOk ? "Glasses ready" : "Connecting your glasses")
                .font(.system(size: Theme.t60Size, weight: .medium))
                .tracking(Theme.t60Tracking)
                .frame(height: Theme.t60Line)
                .textShadow()
                .scaleEffect(0.97 + 0.03 * bump)
                .opacity(min(1, 0.55 + 0.45 * bump))
                .textIn(t, delay: 0.125)
            VStack(spacing: 0) {
                ForEach(rows.indices, id: \.self) { i in
                    row(i, t: t, delay: 0.18 + 0.165 * Double(i))
                    if i < rows.count - 1 {
                        Rectangle().fill(.white.opacity(0.1)).frame(height: 1)
                    }
                }
            }
            .padding(.top, 18)
        }
        .foregroundStyle(.white)
        .frame(width: 640)
    }

    private func row(_ i: Int, t: Double, delay: Double) -> some View {
        let r = rows[i]
        let style = Theme.syncStyle(for: r.state)
        let value = i == 0 && registerNudge && isRegistering
            ? "Approve in Meta AI" : r.value
        return HStack(spacing: 18) {
            syncIcon(style.icon, t: t)
            Text(Self.titles[i])
                .font(Theme.t30)
                .tracking(Theme.t30Tracking)
                .frame(height: Theme.t30Line)
                .textShadow()
                .textIn(t, delay: delay + 0.055)
            Spacer(minLength: 18)
            Text(value)
                .font(Theme.t24)
                .tracking(Theme.t24Tracking)
                .foregroundStyle(.white.opacity(0.62))
                .lineLimit(1)
                .frame(height: Theme.t24Line)
                .textShadow()
                .textIn(t, delay: delay + 0.11)
        }
        .padding(.vertical, 18)
        .padding(.horizontal, 4)
        .opacity(style.rowOpacity)
        .animation(Theme.ease(duration: 0.5), value: r.state)
        .textIn(t, delay: delay)
        .contentShape(Rectangle())
        .onTapGesture { fix(i) }
        .contextMenu {
            if i == 0, let dat, dat.registrationState == .registered {
                Button("Unregister", role: .destructive) { dat.unregister() }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityHint(r.state == .ok ? "" : (r.hint ?? ""))
    }

    /// Row tap = the old per-row button: register, or check/enable camera.
    private func fix(_ i: Int) {
        guard let dat else { return }
        if i == 0, canRegister {
            dat.register()
        } else if i == 2, dat.cameraPermission != .granted {
            if dat.cameraPermission == nil {
                dat.checkCameraPermission()
            } else {
                dat.askCameraRedirect()
            }
        }
    }

    /// `.sico` 26px: hollow (pending), top-arc spin .8s (on), white disc
    /// with a rotated blue L tick (ok).
    private func syncIcon(_ icon: Theme.SyncStyle.SyncIcon, t: Double) -> some View {
        ZStack {
            switch icon {
            case .hollow:
                Circle().strokeBorder(.white.opacity(0.35), lineWidth: 2)
            case .spin:
                ZStack {
                    Circle().strokeBorder(.white.opacity(0.22), lineWidth: 2)
                    Circle().inset(by: 1)
                        .trim(from: 0.625, to: 0.875)
                        .stroke(.white, lineWidth: 2)
                }
                .rotationEffect(.degrees(360 * (t / Theme.spin)
                    .truncatingRemainder(dividingBy: 1)))
            case .tick:
                Circle().fill(.white)
                    .shadow(color: Color(hex: 0x002878).opacity(0.25), radius: 5, y: 2)
                // ::after: left 8 top 4 (inside a 2px border), 6x12 box,
                // right+bottom 2.5px borders, rotate 45deg
                Path { p in
                    p.move(to: CGPoint(x: 0, y: 13.25))
                    p.addLine(to: CGPoint(x: 7.25, y: 13.25))
                    p.addLine(to: CGPoint(x: 7.25, y: 0))
                }
                .stroke(Theme.polishBlue, style: StrokeStyle(lineWidth: 2.5,
                                                             lineJoin: .miter))
                .frame(width: 8.5, height: 14.5)
                .rotationEffect(.degrees(45))
                .position(x: 14.25, y: 13.25)
            }
        }
        .frame(width: 26, height: 26)
    }

    private var footer: some View {
        HStack(spacing: 48) {
            Button("Retry") { refreshAll() }
            Button("Skip") { onSkip() }
        }
        .buttonStyle(.plain)
        .font(Theme.t24)
        .tracking(Theme.t24Tracking)
        .foregroundStyle(.white.opacity(0.62))
        .textShadow()
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
        hfpName = SyncGate.liveHFPName
    }
}

private extension View {
    /// `.sync` text-shadow 0 4px 40px rgba(0,0,0,.25).
    func textShadow() -> some View {
        shadow(color: .black.opacity(0.25), radius: 20, y: 4)
    }
}
