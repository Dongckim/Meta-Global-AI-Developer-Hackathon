import SwiftUI

/// Voice, rate and pitch come only from pip_voice.json (`Persona`), so
/// there is nothing voice-related to pick here.
struct VoiceSettingsView: View {
    @State private var wakeEnabled = VoiceSession.wakeEnabled
    /// Opens the glasses & permissions (sync) screen.
    var onOpenSync: () -> Void = {}
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Button("Glasses & permissions") { onOpenSync() }
                Toggle("“\(Persona.current.wakePhrase)” detection", isOn: $wakeEnabled)
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
            .onChange(of: wakeEnabled) { _, on in VoiceSession.wakeEnabled = on }
        }
    }
}
