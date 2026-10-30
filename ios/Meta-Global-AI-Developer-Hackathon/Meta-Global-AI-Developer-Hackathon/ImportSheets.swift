import SwiftUI
import UniformTypeIdentifiers

// The text import sheet, opened by the registry (screens.json import_text
// `ui`): the words and the tap both open it; what it collects goes up the
// one import route (PipClient.importProtocol). Photos: ScreenParts' camera.

/// Paste or type a protocol, or pick a .txt file.
struct TextImportSheet: View {
    var onImport: (_ text: String, _ filename: String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var filename = ""
    @State private var picking = false

    var body: some View {
        NavigationStack {
            TextEditor(text: $text)
                .font(.body)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 12).fill(Theme.wellEmpty))
                .padding()
                .navigationTitle(filename.isEmpty ? "Paste or type" : filename)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .secondaryAction) {
                        Button("Choose a .txt file", systemImage: "doc.text") { picking = true }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Import") {
                            onImport(text, filename)
                            dismiss()
                        }
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
                .fileImporter(isPresented: $picking, allowedContentTypes: [.plainText]) { res in
                    guard case .success(let url) = res, url.startAccessingSecurityScopedResource() else { return }
                    defer { url.stopAccessingSecurityScopedResource() }
                    if let s = try? String(contentsOf: url, encoding: .utf8) {
                        text = s
                        filename = url.lastPathComponent
                    }
                }
        }
    }
}
