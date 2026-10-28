// GlassesDAT.swift
// Meta Wearables DAT registration + connection status (stage 2: no capture).
// Single @MainActor owner; views read only. Call patterns mirror the official
// CameraAccess sample (WearablesViewModel / RegistrationView).

import Foundation
import MWDATCore
import Observation
import SwiftUI
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

        registrationTask = Task {
            for await state in wearables.registrationStateStream() {
                self.registrationState = state
                self.clearError()
            }
        }
        deviceStreamTask = Task {
            for await ids in wearables.devicesStream() {
                self.devices = ids
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
        Task {
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
        Task {
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
