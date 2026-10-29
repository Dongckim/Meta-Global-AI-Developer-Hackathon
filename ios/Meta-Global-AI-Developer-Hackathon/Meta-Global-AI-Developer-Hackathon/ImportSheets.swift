import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

// Import sheets opened by the registry (screens.json import_text /
// import_photo `ui`): the words and the tap both open them; what they
// collect goes up the one import route (PipClient.importProtocol).

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

/// Photograph protocol pages or pick photos; several photos are pages, in order.
struct PhotoImportSheet: View {
    var onImport: (_ pages: [Data]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var pages: [UIImage] = []
    @State private var picks: [PhotosPickerItem] = []
    @State private var camera = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if pages.isEmpty {
                    ContentUnavailableView("Snap the pages", systemImage: "camera",
                                           description: Text("One photo per page, in order."))
                } else {
                    ScrollView(.horizontal) {
                        HStack(spacing: 12) {
                            ForEach(Array(pages.enumerated()), id: \.offset) { i, img in
                                Image(uiImage: img).resizable().scaledToFit().frame(height: 260)
                                    .overlay(alignment: .topLeading) {
                                        Text("\(i + 1)").font(.caption.bold()).padding(6)
                                            .background(Circle().fill(.white))
                                    }
                            }
                        }
                        .padding(.horizontal)
                    }
                }
                HStack(spacing: 16) {
                    if UIImagePickerController.isSourceTypeAvailable(.camera) {
                        Button("Take a photo", systemImage: "camera") { camera = true }
                            .buttonStyle(.bordered)
                    }
                    PhotosPicker(selection: $picks, matching: .images) {
                        Label("Choose photos", systemImage: "photo.on.rectangle")
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(.vertical)
            .navigationTitle("Photo import")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Import") {
                        onImport(pages.compactMap { $0.jpegData(compressionQuality: 0.8) })
                        dismiss()
                    }
                    .disabled(pages.isEmpty)
                }
            }
            .onChange(of: picks) { _, items in
                Task { @MainActor in
                    for item in items {
                        if let d = try? await item.loadTransferable(type: Data.self), let img = UIImage(data: d) {
                            pages.append(img)
                        }
                    }
                    picks = []
                }
            }
            .fullScreenCover(isPresented: $camera) {
                CameraCapture { img in if let img { pages.append(img) } }.ignoresSafeArea()
            }
        }
    }
}

/// The system camera, one shot per open.
private struct CameraCapture: UIViewControllerRepresentable {
    var onDone: (UIImage?) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let c = UIImagePickerController()
        c.sourceType = .camera
        c.delegate = context.coordinator
        return c
    }

    func updateUIViewController(_ c: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coord { Coord(onDone: onDone) }

    final class Coord: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        var onDone: (UIImage?) -> Void
        init(onDone: @escaping (UIImage?) -> Void) { self.onDone = onDone }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            onDone(info[.originalImage] as? UIImage)
            picker.dismiss(animated: true)
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            onDone(nil)
            picker.dismiss(animated: true)
        }
    }
}
