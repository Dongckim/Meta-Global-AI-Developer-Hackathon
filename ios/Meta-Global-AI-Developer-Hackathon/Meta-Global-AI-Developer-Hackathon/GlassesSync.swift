import AVFoundation
import MWDATCore
import Speech
import SwiftUI

/// Sync screen rows in display order: Bluetooth and the local network,
/// then the glasses area. The mic permission (iOS needs it for the
/// glasses' HFP mic too) is asked from the Glasses audio row, along with
/// speech recognition for "Hey Pip".
enum SyncItem: Int, CaseIterable {
    case bluetooth, localNetwork, metaApp, glasses, audio, camera

    var title: String {
        switch self {
        case .bluetooth: "Bluetooth"
        case .localNetwork: "Local network"
        case .metaApp: "Meta AI app"
        case .glasses: "Glasses"
        case .audio: "Glasses audio"
        case .camera: "Camera"
        }
    }

    /// The row's own OS permission (Glasses audio owns two, see
    /// `SyncRows.glassesAudio`).
    var permission: AppPermission? {
        switch self {
        case .bluetooth: .bluetooth
        case .localNetwork: .localNetwork
        default: nil
        }
    }
}

/// Server reachability as the Local network row shows it (the OS gives no
/// local network status, so the row reports the connection instead).
enum ServerLink: Equatable { case idle, connecting, connected, failed }

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

    /// Bluetooth: tap asks, denied opens Settings, allowed ticks.
    static func permission(_ state: AppPermission.State) -> Row {
        switch state {
        case .notAsked: Row(state: .action, value: "Tap to allow", hint: nil)
        case .denied: Row(state: .failed, value: "Denied · Open Settings",
                          hint: "Allow it in the Settings app.")
        case .allowed: Row(state: .ok, value: "Allowed", hint: nil)
        }
    }

    /// Local network: the first server request is what prompts, so the row
    /// shows that request's outcome.
    static func localNetwork(allowed: Bool, link: ServerLink) -> Row {
        switch link {
        case .connected: Row(state: .ok, value: "Connected", hint: nil)
        case .connecting: Row(state: .checking, value: "Connecting…", hint: nil)
        case .failed: Row(state: .failed, value: "Unreachable · Tap to retry",
                          hint: "Check the \(Persona.current.agentName) server and the local network permission.")
        case .idle: allowed
            ? Row(state: .checking, value: "Connecting…", hint: nil)
            : Row(state: .action, value: "Tap to allow", hint: nil)
        }
    }

    /// DAT rows before Bluetooth is allowed (DAT is not configured yet).
    static let needsBluetooth = Row(state: .waiting, value: "Allow Bluetooth first", hint: nil)

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
                        battery: Int?, error: String?,
                        searching: Bool = false) -> Row {
        if let error {
            return Row(state: .failed, value: "Error", hint: error)
        }
        switch status {
        case .connected:
            let device = name ?? "Glasses"
            let v = battery.map { "\(device) · \($0)%" } ?? device
            return Row(state: .ok, value: v, hint: nil)
        case .found:
            return Row(state: .failed, value: "Wake your glasses",
                       hint: "Found, not connected — put them on or open the case.")
        case .registered:
            if searching {
                return Row(state: .checking, value: "Searching nearby…", hint: nil)
            }
            return Row(state: .failed, value: "Connect your glasses",
                       hint: "Pair the glasses in the Meta AI app.")
        case .unregistered:
            return Row(state: .waiting, value: "Link Meta AI first", hint: nil)
        case .error:
            return Row(state: .failed, value: "Error", hint: nil)
        }
    }

    /// Glasses audio: the mic permission first, then it follows the
    /// glasses: their HFP mic on the route is ok; connected glasses on
    /// another route need Control Center; otherwise it waits for them.
    static func glassesAudio(mic: AppPermission.State, glassesConnected: Bool,
                             hfpName: String?) -> Row {
        switch mic {
        case .notAsked:
            return Row(state: .action, value: "Tap to allow mic", hint: nil)
        case .denied:
            return Row(state: .failed, value: "Allow mic in Settings",
                       hint: "Turn on Microphone for this app in Settings.")
        case .allowed:
            if hfpName != nil { return Row(state: .ok, value: "Glasses mic", hint: hfpName) }
            if glassesConnected {
                return Row(state: .failed, value: "Pick glasses mic",
                           hint: "Choose the glasses as audio in Control Center.")
            }
            return Row(state: .waiting, value: "Connect glasses first", hint: nil)
        }
    }

    static func camera(_ permission: PermissionStatus?,
                       step: GlassesDAT.CameraStep? = nil) -> Row {
        if step == .asking {
            return Row(state: .checking, value: "Asking…", hint: nil)
        }
        if step == .checking, permission == nil {
            return Row(state: .checking, value: "Checking…", hint: nil)
        }
        switch permission {
        case .granted:
            return Row(state: .ok, value: "Allowed", hint: nil)
        case .denied:
            // DAT has no "not asked": denied also means never requested
            // (or an "Allow once" that expired), so offer the grant flow.
            return Row(state: .action, value: "Tap to allow",
                       hint: "Allow the camera in the Meta AI app.")
        case .none:
            return Row(state: .failed, value: "Not checked",
                       hint: "Check the DAT camera permission.")
        }
    }
}

/// Headless sync evaluation for the launch gate: the same rows the
/// screen renders.
enum SyncGate {
    /// All eight rows in `SyncItem` order.
    static func rows(permission: (AppPermission) -> AppPermission.State,
                     server: ServerLink, dat: GlassesDAT?,
                     hfpName: String?) -> [SyncRows.Row] {
        SyncItem.allCases.map { item in
            switch item {
            case .bluetooth:
                return SyncRows.permission(permission(.bluetooth))
            case .localNetwork:
                return SyncRows.localNetwork(
                    allowed: permission(.localNetwork) == .allowed, link: server)
            case .audio:
                return SyncRows.glassesAudio(mic: permission(.microphone),
                                             glassesConnected: dat?.status == .connected,
                                             hfpName: hfpName)
            case .metaApp, .glasses, .camera:
                guard let dat else { return SyncRows.needsBluetooth }
                switch item {
                case .metaApp:
                    return SyncRows.app(registration: dat.registrationState,
                                        error: dat.lastError)
                case .glasses:
                    return SyncRows.glasses(
                        status: dat.status, name: dat.deviceName,
                        battery: dat.batteryLevel,
                        error: dat.status == .error ? dat.lastError : nil,
                        searching: !dat.devicesReported)
                default:
                    return SyncRows.camera(dat.cameraPermission, step: dat.cameraStep)
                }
            }
        }
    }

    static var liveHFPName: String? {
        AVAudioSession.sharedInstance().currentRoute.inputs.first {
            $0.portType == .bluetoothHFP
        }?.portName
    }

    /// Launch gate: a row permission (Bluetooth, local network, mic) not
    /// allowed, or the glasses not ready and not skipped (Skip covers glasses
    /// rows only). Speech only gates "Hey Pip"; the voice line says so.
    static func needed(permissions: [AppPermission.State],
                       glasses: [SyncRows.Row], skipped: Bool) -> Bool {
        permissions.contains { $0 != .allowed }
            || (!skipped && !glasses.allSatisfy { $0.state == .ok })
    }

    /// Retry/Skip: only once a row failed, or a step stayed pending 10s+.
    static func showsActions(rows: [SyncRows.Row], pendingFor: TimeInterval?) -> Bool {
        rows.contains { $0.state == .failed } || (pendingFor ?? 0) > 10
    }
}

/// Vertical fit for the sync block on the 1376x1032 canvas: when the rows
/// don't fit, only row padding and the list's top margin shrink (type and
/// icon sizes stay at design values).
enum SyncLayout {
    /// Canvas height left for the centered block: .scr padding 44 plus the
    /// bottom Retry/Skip line (and the same space mirrored on top).
    static let available: CGFloat = Theme.canvasHeight - 2 * (44 + Theme.t24Line + 24)

    /// (row vertical padding, list top margin) for `rows` rows.
    static func spacing(rows: Int, available: CGFloat = available) -> (row: CGFloat, top: CGFloat) {
        let n = CGFloat(rows)
        // glasses 60 + gap 26 + title + gap 26 + rows + 1px dividers
        let fixed = 60 + 26 + Theme.t60Line + 26 + n * Theme.t30Line + max(0, n - 1)
        let flexible = 18 + n * 2 * 18
        let k = min(1, max(0, (available - fixed) / flexible))
        return (18 * k, 18 * k)
    }
}


/// Fullscreen Glasses sync step (design `data-id="sync"`), which is also
/// where every OS permission is asked: eight rows on the dark aurora
/// canvas. A row tap is its one action (ask, open Settings, connect,
/// register, camera). Auto-advances shortly after all rows turn ok
/// ("Glasses ready"); Retry/Skip show once a row failed or a step stays
/// pending past 10s.
struct GlassesSyncScreen: View {
    var dat: GlassesDAT?
    var voice: VoiceSession? = nil
    /// Local network row inputs (owned by the app, which talks to the server).
    var serverAllowed = false
    var server: ServerLink = .idle
    /// Bluetooth tap: configures DAT, which shows the Bluetooth prompt.
    var onAllowBluetooth: () -> Void = {}
    /// Local network tap: the server request that shows its prompt.
    var onConnectServer: () -> Void = {}
    /// Bumped by voice "retry the glasses check" (server sync_retry).
    var refreshGen: Int = 0
    var onSkip: () -> Void
    var onComplete: () -> Void
    #if DEBUG
    /// Fixed rows for design captures (`-uiPreview connecting|ready`).
    var previewRows: [SyncRows.Row]?
    @State private var showDiagnostics = false
    #endif
    /// Permission prompts make the app inactive; the phase change (and this
    /// counter, after a request returns) re-reads permission states.
    @Environment(\.scenePhase) private var scenePhase
    @State private var permissionGen = 0
    @State private var hfpName: String?
    @State private var registerNudge = false
    @State private var clock = FrameClock()
    /// Clock time when every row turned ok (drives readyGlow / bump).
    @State private var readyAt: Double?
    /// When some row last started pending (for the 10s Retry/Skip rule).
    @State private var pendingSince: Date?

    private var rows: [SyncRows.Row] {
        #if DEBUG
        if let previewRows { return previewRows }
        #endif
        _ = (scenePhase, permissionGen)
        return SyncGate.rows(permission: { $0.state(serverAllowed: serverAllowed) },
                             server: server, dat: dat, hfpName: hfpName)
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
            let t = clock.tick(ctx.date)
            DarkCanvas {
                ZStack {
                    AuroraView(mode: readyAt.map { .syncReady(since: $0) } ?? .sync, t: t)
                    syncBlock(t)
                    if SyncGate.showsActions(
                        rows: rows,
                        pendingFor: pendingSince.map { ctx.date.timeIntervalSince($0) }) {
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
        #if DEBUG
        .sheet(isPresented: $showDiagnostics) { DiagnosticsView(voice: voice) }
        #endif
        .onAppear {
            if allOk { readyAt = 0 }
            refreshAudio()
            dat?.checkCameraPermission()
        }
        .onChange(of: refreshGen) { _, _ in refreshAll() }
        .onReceive(NotificationCenter.default.publisher(
            for: AVAudioSession.routeChangeNotification)) { _ in refreshAudio() }
        .onChange(of: allOk) { _, ok in
            readyAt = ok ? clock.t : nil
        }
        .onChange(of: rows.contains { $0.state == .checking }, initial: true) { _, pending in
            pendingSince = pending ? (pendingSince ?? Date()) : nil
        }
        .task(id: allOk) {
            guard allOk else { return }
            do {
                // design runSync: records 1.5s after "Glasses ready"
                try await Task.sleep(for: .seconds(1.5))
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
    /// prepare(): 55ms apart from 70ms, three candidates per row.
    private func syncBlock(_ t: Double) -> some View {
        let sinceReady = readyAt.map { t - $0 }
        // .glasses scale .6s spring to 1.12; title bump .55s spring
        let grow = sinceReady.map {
            Keyframes.tween($0, duration: 0.6, curve: Keyframes.spring)
        } ?? 0
        let bump = sinceReady.map {
            Keyframes.tween($0, duration: 0.55, curve: Keyframes.spring)
        } ?? 1
        let fit = SyncLayout.spacing(rows: rows.count)
        return VStack(spacing: 26) {
            glassesIcon
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
                    row(i, t: t, delay: 0.18 + 0.165 * Double(i), pad: fit.row)
                    if i < rows.count - 1 {
                        Rectangle().fill(.white.opacity(0.1)).frame(height: 1)
                    }
                }
            }
            .padding(.top, fit.top)
        }
        .foregroundStyle(.white)
        .frame(width: 640)
    }

    private var glassesIcon: some View {
        GlassesIcon()
            .stroke(.white, style: StrokeStyle(lineWidth: 4.8, lineCap: .round,
                                               lineJoin: .round))
            .frame(width: 136, height: 60)
            #if DEBUG
            .contentShape(Rectangle())
            .onLongPressGesture { showDiagnostics = true }
            #endif
    }

    private func row(_ i: Int, t: Double, delay: Double, pad: CGFloat) -> some View {
        let r = rows[i]
        let item = SyncItem(rawValue: i) ?? .camera
        let style = Theme.syncStyle(for: r.state)
        let value = item == .metaApp && registerNudge && isRegistering
            ? "Approve in Meta AI" : r.value
        return HStack(spacing: 18) {
            syncIcon(style.icon, t: t)
            Text(item.title)
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
        .padding(.vertical, pad)
        .padding(.horizontal, 4)
        .opacity(style.rowOpacity)
        .animation(Theme.ease(duration: 0.5), value: r.state)
        .textIn(t, delay: delay)
        .contentShape(Rectangle())
        .onTapGesture { tap(item, r) }
        .contextMenu {
            if item == .metaApp, let dat, dat.registrationState == .registered {
                Button("Unregister", role: .destructive) { dat.unregister() }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(r.state == .ok ? [] : .isButton)
        .accessibilityHint(r.state == .ok ? "" : (r.hint ?? value))
    }

    /// A row's one action: ask (Bluetooth; Glasses audio asks mic then
    /// speech), open Settings when denied, connect, or the glasses fixes.
    private func tap(_ item: SyncItem, _ r: SyncRows.Row) {
        switch item {
        case .bluetooth:
            if r.state == .action { onAllowBluetooth() }
            if r.state == .failed { openSettings() }
        case .localNetwork:
            if r.state == .action || r.state == .failed { onConnectServer() }
        case .audio:
            let mic = AppPermission.microphone.state(serverAllowed: serverAllowed)
            let speech = AppPermission.speech.state(serverAllowed: serverAllowed)
            if mic == .denied {
                openSettings()
            } else if r.state == .action {
                Task { @MainActor in
                    if mic == .notAsked { _ = await Recorder.requestPermission() }
                    if speech == .notAsked {
                        _ = await withCheckedContinuation { cont in
                            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0) }
                        }
                    }
                    permissionsChanged()
                }
            }
        case .metaApp:
            if canRegister { dat?.register() }
        case .camera:
            guard let dat, dat.cameraPermission != .granted else { return }
            if dat.cameraPermission == nil {
                dat.checkCameraPermission()
            } else {
                dat.askCameraRedirect()
            }
        case .glasses:
            break
        }
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) {
            UIApplication.shared.open(url)
        }
    }

    private func permissionsChanged() {
        permissionGen += 1
        voice?.refresh()
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
        permissionGen += 1
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
