import Foundation
import MWDATCore
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

private struct FakeToken: AnyListenerToken {
    func cancel() async {}
}

/// Scriptable stand-in for the DAT SDK. Device is a closed SDK class, so
/// deviceForIdentifier always returns nil (link/connected paths are covered
/// through deriveStatus + the found stage).
private final class FakeWearables: WearablesInterface, @unchecked Sendable {
    var registrationState: RegistrationState = .unavailable
    var devices: [DeviceIdentifier] = []
    var calls: [String] = []
    var startRegistrationError: RegistrationError?
    var startUnregistrationError: UnregistrationError?
    var permissionStatus: PermissionStatus = .denied
    private var regCont: AsyncStream<RegistrationState>.Continuation?
    private var devCont: AsyncStream<[DeviceIdentifier]>.Continuation?

    func pushRegistration(_ s: RegistrationState) {
        registrationState = s
        regCont?.yield(s)
    }

    func pushDevices(_ d: [DeviceIdentifier]) {
        devices = d
        devCont?.yield(d)
    }

    func addRegistrationStateListener(
        _ listener: @escaping @Sendable (RegistrationState) -> Void
    ) -> AnyListenerToken { FakeToken() }

    func registrationStateStream() -> AsyncStream<RegistrationState> {
        AsyncStream { self.regCont = $0 }
    }

    func startRegistration() async throws(RegistrationError) {
        calls.append("startRegistration")
        if let e = startRegistrationError { throw e }
    }

    func handleUrl(_ url: URL) async throws(WearablesHandleURLError) -> Bool {
        calls.append("handleUrl")
        return true
    }

    func startUnregistration() async throws(UnregistrationError) {
        calls.append("startUnregistration")
        if let e = startUnregistrationError { throw e }
    }

    func openFirmwareUpdate() async throws(NavigationError) {}
    func openDATGlassesAppUpdate() async throws(NavigationError) {}

    func addDevicesListener(
        _ listener: @escaping @Sendable ([DeviceIdentifier]) -> Void
    ) -> AnyListenerToken { FakeToken() }

    func devicesStream() -> AsyncStream<[DeviceIdentifier]> {
        AsyncStream { self.devCont = $0 }
    }

    func deviceForIdentifier(_ identifier: DeviceIdentifier) -> Device? { nil }

    func checkPermissionStatus(_ permission: Permission) async throws(PermissionError) -> PermissionStatus {
        calls.append("checkPermissionStatus")
        return permissionStatus
    }

    func requestPermission(_ permission: Permission) async throws(PermissionError) -> PermissionStatus {
        calls.append("requestPermission")
        return permissionStatus
    }

    func createSession(deviceSelector: any DeviceSelector) throws(DeviceSessionError) -> DeviceSession {
        throw DeviceSessionError.noEligibleDevice
    }
}

@MainActor
struct GlassesDATTests {
    private func settle(_ ns: UInt64 = 100_000_000) async {
        try? await Task.sleep(nanoseconds: ns)
    }

    private func makeDAT(_ fake: FakeWearables) async -> GlassesDAT {
        let dat = GlassesDAT(wearables: fake)
        await settle() // let init subscribe to both streams
        return dat
    }

    @Test func deriveStatusMatrix() {
        typealias S = GlassesDAT.LinkStatus
        #expect(GlassesDAT.deriveStatus(registration: .unavailable, deviceCount: 0, connected: false, hasError: false) == .unregistered)
        #expect(GlassesDAT.deriveStatus(registration: .available, deviceCount: 0, connected: false, hasError: false) == .unregistered)
        #expect(GlassesDAT.deriveStatus(registration: .registering, deviceCount: 0, connected: false, hasError: false) == .unregistered)
        #expect(GlassesDAT.deriveStatus(registration: .registered, deviceCount: 0, connected: false, hasError: false) == .registered)
        #expect(GlassesDAT.deriveStatus(registration: .registered, deviceCount: 1, connected: false, hasError: false) == .found)
        #expect(GlassesDAT.deriveStatus(registration: .registered, deviceCount: 1, connected: true, hasError: false) == .connected)
        #expect(GlassesDAT.deriveStatus(registration: .registered, deviceCount: 0, connected: false, hasError: true) == .error)
        #expect(GlassesDAT.deriveStatus(registration: .unavailable, deviceCount: 0, connected: false, hasError: true) == .error)
        _ = S.unregistered
    }

    @Test func streamTransitionsAndErrorClear() async {
        let fake = FakeWearables()
        let dat = await makeDAT(fake)
        #expect(dat.status == .unregistered)
        fake.pushRegistration(.registered)
        await settle()
        #expect(dat.status == .registered)
        fake.pushDevices(["d1"])
        await settle()
        #expect(dat.status == .found)
        dat.noteError("boom")
        #expect(dat.status == .error)
        fake.pushDevices([])
        await settle()
        #expect(dat.status == .registered)
    }

    @Test func registerGuardWhileRegistering() async {
        let fake = FakeWearables()
        let dat = await makeDAT(fake)
        fake.pushRegistration(.registering)
        await settle()
        #expect(dat.busy)
        dat.register()
        await settle()
        #expect(!fake.calls.contains("startRegistration"))
        fake.pushRegistration(.available)
        await settle()
        dat.register()
        await settle()
        #expect(fake.calls.contains("startRegistration"))
    }

    @Test func registerErrorSurfacesReason() async {
        let fake = FakeWearables()
        fake.startRegistrationError = .metaAINotInstalled
        let dat = await makeDAT(fake)
        fake.pushRegistration(.available)
        await settle()
        dat.register()
        await settle()
        #expect(dat.status == .error)
        #expect(dat.lastError == RegistrationError.metaAINotInstalled.description)
    }

    @Test func callbackFiltersNonDATTURLs() async {
        let fake = FakeWearables()
        let dat = await makeDAT(fake)
        dat.handleCallback(URL(string: "pipglass://open")!)
        await settle()
        #expect(!fake.calls.contains("handleUrl"))
        dat.handleCallback(URL(string: "pipglass://cb?metaWearablesAction=reg")!)
        await settle()
        #expect(fake.calls.contains("handleUrl"))
    }

    @Test func cameraPermissionPassthrough() async {
        let fake = FakeWearables()
        fake.permissionStatus = .granted
        let dat = await makeDAT(fake)
        dat.checkCameraPermission()
        await settle()
        #expect(dat.cameraPermission == .granted)
        #expect(fake.calls.contains("checkPermissionStatus"))
    }
}
