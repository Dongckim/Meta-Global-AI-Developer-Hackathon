import SwiftUI
import OSLog
#if DEBUG
import MWDATMockDevice
#endif

/// `-demo`: no glasses, no Meta account, no key (README, Try it without hardware). Mock Device
/// Kit stands in for the glasses and a text box for the mic. Debug builds only.
enum DemoMode {
    static var isOn: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-demo")
        #else
        false
        #endif
    }

    /// Before the first frame: the sync gate's rows are about hardware the demo doesn't have.
    static func prepare() {
        guard isOn else { return }
        UserDefaults.standard.set(true, forKey: "pip.serverAllowed")
        UserDefaults.standard.set(true, forKey: "pip.syncSkipped")
    }
}

#if DEBUG
extension GlassesDAT {
    /// Pairs mock glasses, worn, showing the demo feed, and opens the stream once they connect.
    func startDemo() {
        setMockEnabled(true)
        pairMockGlasses()
        guard let feed = Bundle.main.url(forResource: "DemoFeed", withExtension: "mov") else {
            noteError("DemoFeed.mov is missing from the app")
            return
        }
        for case let glasses as any MockGlasses in MockDeviceKit.shared.pairedDevices {
            glasses.services.camera.setCameraFeed(fileURL: feed)
            glasses.powerOn()
            glasses.unfold()
            glasses.don()
        }
        Task { @MainActor in
            for tries in 0...15 {  // every 2 s for 30 s
                if status == .connected {
                    Log.dat.info("[demo] glasses connected after \(tries * 2) s")
                    startLive(hold: true)
                    return
                }
                if tries < 15 { try? await Task.sleep(for: .seconds(2)) }
            }
            Log.dat.info("[demo] glasses \(String(describing: self.status), privacy: .public) after 30 s")
            demoNotConnected = true
        }
    }
}

/// The mic's stand-in and the glasses' view, over every screen.
struct DemoBar: View {
    let dat: GlassesDAT?
    let voice: VoiceSession
    @State private var command = ""

    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            Group {
                if let frame = dat?.liveFrame {
                    Image(uiImage: frame).resizable().scaledToFit()
                } else {
                    Color.black.overlay(
                        Text(dat?.demoNotConnected == true ? "Mock glasses not connected — the rest of the app works"
                                                           : "Mock glasses…")
                            .font(.caption).multilineTextAlignment(.center).foregroundStyle(.white).padding(8))
                }
            }
            .frame(width: 108, height: 192)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            TextField("Type what you'd say to Milo", text: $command)
                .textFieldStyle(.roundedBorder)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .frame(width: 320)
                .onSubmit {
                    let text = command.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { return }
                    command = ""
                    voice.type(text)
                }
        }
        .padding(16)
    }
}
#endif
