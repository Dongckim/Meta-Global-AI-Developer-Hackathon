// GlassesDAT.swift
// Meta Wearables DAT registration + connection status, and one-photo capture.
// Single @MainActor owner; views read only. Call patterns mirror the official
// CameraAccess sample (WearablesViewModel / RegistrationView).

import AVFoundation
import Foundation
import os
import MWDATCamera
import MWDATCore
import Observation
import SwiftUI
import UIKit
#if DEBUG
import MWDATMockDevice
#endif

/// Glasses registration + connection status in one place.
@Observable
@MainActor
final class GlassesDAT {
    /// Setup UI stages.
    enum LinkStatus: String, Equatable, Sendable {
        case unregistered
        case registered
        case found
        case connected
        case error
    }

    private(set) var registrationState: RegistrationState
    private(set) var devices: [DeviceIdentifier] = []
    private(set) var deviceName: String?
    private(set) var batteryLevel: Int?
    private(set) var cameraPermission: PermissionStatus?
    /// Camera permission call in flight (sync row shows it as pending).
    enum CameraStep: Equatable { case checking, asking }
    private(set) var cameraStep: CameraStep?
    /// False until the first devicesStream event ("Searching nearby…").
    private(set) var devicesReported = false
    private(set) var lastError: String?
    private(set) var showCameraRedirectConfirm = false
    #if DEBUG
    private(set) var mockEnabled = false
    #endif

    var status: LinkStatus {
        Self.deriveStatus(
            registration: registrationState, deviceCount: devices.count,
            connected: !connectedIds.isEmpty, hasError: lastError != nil)
    }

    var busy: Bool { registrationState == .registering }

    /// Pure stage mapping, covered by unit tests.
    static func deriveStatus(
        registration: RegistrationState, deviceCount: Int,
        connected: Bool, hasError: Bool
    ) -> LinkStatus {
        if hasError { return .error }
        guard registration == .registered else { return .unregistered }
        if connected { return .connected }
        return deviceCount > 0 ? .found : .registered
    }

    private let wearables: WearablesInterface
    @ObservationIgnored private var registrationTask: Task<Void, Never>?
    @ObservationIgnored private var deviceStreamTask: Task<Void, Never>?
    @ObservationIgnored private var linkTokens: [DeviceIdentifier: AnyListenerToken] = [:]
    @ObservationIgnored private var connectedIds: Set<DeviceIdentifier> = []

    init(wearables: WearablesInterface) {
        self.wearables = wearables
        self.registrationState = wearables.registrationState
        self.devices = wearables.devices
        monitorDevices(devices)

        Log.dat.info("[dat] start registration=\(String(describing: self.registrationState), privacy: .public) devices=\(self.devices.count)")
        registrationTask = Task {
            for await state in wearables.registrationStateStream() {
                Log.dat.info("[dat] registration -> \(String(describing: state), privacy: .public)")
                self.registrationState = state
                self.clearError()
            }
        }
        deviceStreamTask = Task {
            for await ids in wearables.devicesStream() {
                Log.dat.info("[dat] devices -> \(ids.count)")
                self.devices = ids
                self.devicesReported = true
                self.monitorDevices(ids)
                self.clearError()
            }
        }
    }

    isolated deinit {
        registrationTask?.cancel()
        deviceStreamTask?.cancel()
    }

    // MARK: - Actions (views call these; views never mutate state)

    /// Opens the Meta AI app for approval; completion returns via URL callback.
    func register() {
        guard registrationState != .registering else { return }
        clearError()
        Task {
            do {
                try await wearables.startRegistration()
            } catch let error as RegistrationError {
                self.noteError(error.description)
            } catch {
                self.noteError(error.localizedDescription)
            }
        }
    }

    func unregister() {
        clearError()
        Task {
            do {
                try await wearables.startUnregistration()
            } catch let error as UnregistrationError {
                self.noteError(error.description)
            } catch {
                self.noteError(error.localizedDescription)
            }
        }
    }

    /// Handles the Meta AI app callback. Ignores URLs without the DAT action
    /// query item, like the sample's RegistrationView.
    func handleCallback(_ url: URL) {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.queryItems?.contains(where: { $0.name == "metaWearablesAction" }) == true
        else { return }
        Task {
            do {
                _ = try await wearables.handleUrl(url)
            } catch {
                self.noteError(error.localizedDescription)
            }
        }
    }

    /// Stage 2 wires permission status only; no capture or streaming yet.
    func checkCameraPermission() {
        cameraStep = .checking
        Task {
            defer { if self.cameraStep == .checking { self.cameraStep = nil } }
            do {
                self.cameraPermission = try await wearables.checkPermissionStatus(.camera)
            } catch {
                self.noteError(error.localizedDescription)
            }
        }
    }

    /// Asks the user to confirm the Meta AI app switch before requesting.
    func askCameraRedirect() {
        checkCameraPermission()
        showCameraRedirectConfirm = cameraPermission != .granted
    }

    func setCameraRedirectConfirm(_ on: Bool) {
        showCameraRedirectConfirm = on
    }

    /// Requesting switches to the Meta AI app; the sample confirms first.
    func requestCameraPermission() {
        showCameraRedirectConfirm = false
        cameraStep = .asking
        Task {
            defer { self.cameraStep = nil }
            do {
                self.cameraPermission = try await wearables.requestPermission(.camera)
            } catch {
                self.noteError(error.localizedDescription)
            }
        }
    }

    #if DEBUG
    func setMockEnabled(_ on: Bool) {
        if on {
            MockDeviceKit.shared.enable()
        } else {
            MockDeviceKit.shared.disable()
        }
        mockEnabled = MockDeviceKit.shared.isEnabled
    }

    func pairMockGlasses() {
        do {
            _ = try MockDeviceKit.shared.pairGlasses(model: .rayBanMeta)
            clearError()
        } catch {
            noteError(error.localizedDescription)
        }
    }

    /// Paired mock glasses may start powered off; this drives found→connected.
    func powerOnMockDevices() {
        for device in MockDeviceKit.shared.pairedDevices {
            device.powerOn()
        }
    }
    #endif

    func noteError(_ message: String) {
        lastError = message
    }

    // MARK: - Live view and photo (a photo question)
    // As Meta's working sample on the same Personal Team does it (Rayban-Demo
    // GlassDemo/CameraModel): the camera stream at medium, 15 fps, over
    // Bluetooth (Info.plist declares the accessory protocol); the photo is
    // taken from that stream. Standalone photo
    // capture failed here (sessionSetupFailed: Capture service unavailable),
    // and a high stream waited for the glasses' Wi-Fi (Hotspot entitlement,
    // which a Personal Team can't sign: see the .entitlements file).

    enum PhotoFailure: Error { case notReady(String), timedOut }

    /// What the glasses see while a photo question waits (stream frames).
    private(set) var liveFrame: UIImage?
    private var liveSession: DeviceSession?
    private var liveStream: MWDATCamera.Stream?
    private var liveTokens = ListenerTokenBag()
    private var livePhoto: OnceBox<Data?>?

    /// Starts the glasses' camera stream (glasses only); frames show as the live view.
    func startLive() {
        guard liveSession == nil else { return }
        liveFrame = nil
        guard let id = devices.first(where: { wearables.deviceForIdentifier($0)?.linkState == .connected }) else {
            Log.dat.info("[look] live: no connected glasses (devices=\(self.devices.count))")
            return
        }
        Task { @MainActor in
            do {
                if cameraPermission != .granted {
                    cameraPermission = try await wearables.checkPermissionStatus(.camera)
                }
                guard cameraPermission == .granted else {
                    Log.dat.info("[look] live: glasses camera not allowed (\(String(describing: self.cameraPermission), privacy: .public))")
                    askCameraRedirect()
                    return
                }
                // a fresh AutoDeviceSelector hasn't picked a device yet (noEligibleDevice): by id
                let session = try wearables.createSession(deviceSelector: SpecificDeviceSelector(device: id))
                liveSession = session
                let states = session.stateStream()
                try session.start()
                for await st in states {
                    Log.dat.info("[look] live: session \(String(describing: st), privacy: .public)")
                    if st == .started { break }
                    if st == .stopped { throw PhotoFailure.notReady("session stopped") }
                }
                // medium (decided after the iPad test); the sample drops to low while HFP shares the Bluetooth
                let hfp = AVAudioSession.sharedInstance().currentRoute.inputs.contains { $0.portType == .bluetoothHFP }
                let config = StreamConfiguration(videoCodec: .raw, resolution: .medium, frameRate: 15)
                Log.dat.info("[look] live: stream medium / 15 fps (HFP \(hfp ? "on" : "off", privacy: .public))")
                guard liveSession === session, let camera = try session.addCamera(config: config) else {
                    throw PhotoFailure.notReady("no camera")
                }
                let stream = camera.stream
                liveStream = stream
                stream.statePublisher.listen { st in
                    Log.dat.info("[look] live: stream \(String(describing: st), privacy: .public)")
                }.store(in: liveTokens)
                stream.errorPublisher.listen { error in
                    Log.dat.error("[look] live: stream error \(String(describing: error), privacy: .public)")
                }.store(in: liveTokens)
                stream.videoFramePublisher.listen { [weak self] frame in
                    guard let img = frame.makeUIImage() else { return }
                    Task { @MainActor in self?.liveFrame = img }
                }.store(in: liveTokens)
                stream.photoDataPublisher.listen { [weak self] photo in
                    Log.dat.info("[look] photo: received \(photo.data.count) bytes")
                    Task { @MainActor in self?.livePhoto?.set(photo.data) }
                }.store(in: liveTokens)
                stream.start()
            } catch {
                Log.dat.error("[look] live failed: \(String(describing: error), privacy: .public)")
                stopLive()
            }
        }
    }

    func stopLive() {
        guard liveSession != nil else { return }
        livePhoto?.set(nil)
        liveStream?.stop()
        liveSession?.stop()
        liveStream = nil
        liveSession = nil
        liveTokens = ListenerTokenBag()
        liveFrame = nil
        Log.dat.info("[look] live: stopped")
    }

    /// A photo from the running stream (JPEG; the frames are compressed for
    /// Bluetooth, the photo isn't); if it doesn't come, the frame on screen.
    func takePhoto(timeout: Duration = .seconds(5)) async throws -> Data {
        guard let stream = liveStream else { throw PhotoFailure.notReady("no live view") }
        let frame = liveFrame?.jpegData(compressionQuality: 0.9)
        let box = OnceBox<Data?>()
        livePhoto = box
        defer { livePhoto = nil }
        guard stream.capturePhoto(format: .jpeg) else {
            Log.dat.info("[look] photo: capture not available, the live frame instead")
            if let frame { return frame }
            throw PhotoFailure.notReady("capture not available")
        }
        Log.dat.info("[look] photo: capture requested")
        let data: Data? = await withCheckedContinuation { cont in
            box.onSet = { cont.resume(returning: $0) }
            Task { @MainActor in
                try? await Task.sleep(for: timeout)
                box.set(nil)
            }
        }
        if let data { return data }
        Log.dat.info("[look] photo: none in \(timeout, privacy: .public), the live frame instead")
        if let frame { return frame }
        throw PhotoFailure.timedOut
    }

    func clearError() {
        lastError = nil
    }

    // MARK: - Private

    private func monitorDevices(_ ids: [DeviceIdentifier]) {
        let set = Set(ids)
        linkTokens = linkTokens.filter { set.contains($0.key) }
        connectedIds = connectedIds.filter { set.contains($0) }
        refreshFirstDevice(ids)
        for id in ids {
            guard linkTokens[id] == nil,
                  let device = wearables.deviceForIdentifier(id) else { continue }
            if device.linkState == .connected { connectedIds.insert(id) }
            let token = device.addLinkStateListener { [weak self] state in
                Task { [weak self] in await self?.handleLinkState(state, deviceId: id) }
            }
            linkTokens[id] = token
        }
    }

    private func handleLinkState(_ state: LinkState, deviceId: DeviceIdentifier) {
        if state == .connected {
            connectedIds.insert(deviceId)
        } else {
            connectedIds.remove(deviceId)
        }
        refreshFirstDevice(devices)
    }

    private func refreshFirstDevice(_ ids: [DeviceIdentifier]) {
        guard let first = ids.first,
              let device = wearables.deviceForIdentifier(first) else {
            deviceName = nil
            batteryLevel = nil
            return
        }
        deviceName = device.nameOrId()
        batteryLevel = device.batteryLevel
    }
}

/// A value set once from any thread; the first set wins (a photo result vs its timeout).
final class OnceBox<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    var onSet: ((T) -> Void)?

    func set(_ value: T) {
        lock.lock()
        guard !done else { lock.unlock(); return }
        done = true
        let f = onSet
        lock.unlock()
        f?(value)
    }
}
