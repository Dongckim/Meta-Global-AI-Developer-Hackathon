import AVFoundation
import CoreBluetooth
import Speech

/// OS permissions the app needs, read without prompting. Prompts happen
/// only from Setup's per-permission Allow buttons.
enum AppPermission: CaseIterable {
    case bluetooth, microphone, speech, localNetwork

    enum State: Equatable { case notAsked, allowed, denied }

    var title: String {
        switch self {
        case .bluetooth: "Bluetooth"
        case .microphone: "Microphone"
        case .speech: "Speech recognition"
        case .localNetwork: "Local network"
        }
    }

    /// Current state. Local network has no status API: `serverAllowed`
    /// records that the Allow tap already made the prompting request.
    func state(serverAllowed: Bool) -> State {
        switch self {
        case .bluetooth: Self.map(CBManager.authorization)
        case .microphone: Self.map(AVAudioApplication.shared.recordPermission)
        case .speech: Self.map(SFSpeechRecognizer.authorizationStatus())
        case .localNetwork: serverAllowed ? .allowed : .notAsked
        }
    }

    static func anyNotAsked(serverAllowed: Bool) -> Bool {
        allCases.contains { $0.state(serverAllowed: serverAllowed) == .notAsked }
    }

    static func map(_ s: CBManagerAuthorization) -> State {
        switch s {
        case .notDetermined: .notAsked
        case .allowedAlways: .allowed
        default: .denied
        }
    }

    static func map(_ s: AVAudioApplication.recordPermission) -> State {
        switch s {
        case .undetermined: .notAsked
        case .granted: .allowed
        default: .denied
        }
    }

    static func map(_ s: SFSpeechRecognizerAuthorizationStatus) -> State {
        switch s {
        case .notDetermined: .notAsked
        case .authorized: .allowed
        default: .denied
        }
    }
}
