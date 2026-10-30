import AVFoundation
import PhotosUI
import SwiftUI
import WebKit

// Our own screens where iOS's (file picker, camera, Safari) can't be driven by
// voice: the file list (import_file), the camera (import_photo) and a source
// page (web_source). Their buttons are registry `control` / `pick` actions,
// so a tap and the words go through the server the same way and come back
// as a ui `control` / `pick`, handed to the screen through ScreenCommands.
// "Browse files…" and "Choose photos…" stay as iOS pickers (tap only).

/// The latest control command for the screen on top (server ui `control`).
@Observable
@MainActor
final class ScreenCommands {
    struct Command: Equatable { var cmd: String; var n: Int? }
    private(set) var last: Command?
    private(set) var gen = 0

    func send(_ cmd: String, n: Int?) {
        last = Command(cmd: cmd, n: n)
        gen += 1
    }
}

// MARK: - import_file: the app's files

/// A PDF or text file in the app's Documents folder (Files app: On My iPad).
struct LocalFile: Identifiable, Equatable {
    var url: URL
    var date: Date
    var id: String { url.lastPathComponent }
    /// "drug_ab-dosing.pdf" -> "drug ab dosing".
    var name: String {
        url.deletingPathExtension().lastPathComponent
            .replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
    }
    var kind: String { url.pathExtension.lowercased() == "pdf" ? "PDF" : "Text" }

    /// Newest first; folders (the originals kept for protocols) left out.
    static func all() -> [LocalFile] {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey, .isDirectoryKey])) ?? []
        return urls.compactMap { u in
            let v = try? u.resourceValues(forKeys: [.contentModificationDateKey, .isDirectoryKey])
            guard v?.isDirectory != true, ["pdf", "txt"].contains(u.pathExtension.lowercased()) else { return nil }
            return LocalFile(url: u, date: v?.contentModificationDate ?? .distantPast)
        }
        .sorted { $0.date > $1.date }
    }
}

struct FileListScreen: View {
    var files: [LocalFile]
    var buttons: [ScreenButton]
    var u: CGFloat
    var onPick: (Int) -> Void
    var perform: (ScreenButton) -> Void
    /// Rows that fit without scrolling; "number 7" still picks past them.
    private let shown = 6

    var body: some View {
        VStack(spacing: 36 * u) {
            VStack(alignment: .leading, spacing: 0) {
                if files.isEmpty {
                    Text("No PDF or text files here yet.").font(.system(size: 34 * u, weight: .semibold))
                        .foregroundStyle(Theme.text1)
                    Text("In the Files app, put them in On My iPad › \(Persona.appName).")
                        .font(.system(size: 26 * u)).foregroundStyle(Theme.text2).padding(.top, 12 * u)
                }
                ForEach(Array(files.prefix(shown).enumerated()), id: \.element.id) { i, f in
                    Button { onPick(i + 1) } label: {
                        HStack(spacing: 22 * u) {
                            Text("\(i + 1)").font(.system(size: 22 * u, weight: .semibold))
                                .foregroundStyle(Theme.text1)
                                .frame(width: 44 * u, height: 44 * u).background(Circle().fill(Theme.wellEmpty))
                            Image(systemName: f.kind == "PDF" ? "doc.fill" : "text.alignleft")
                                .foregroundStyle(Theme.polishBlue)
                            Text(f.name).font(.system(size: 30 * u, weight: .semibold)).foregroundStyle(Theme.text1)
                                .lineLimit(1).minimumScaleFactor(0.6)
                            Spacer(minLength: 12 * u)
                            Text("\(f.kind) · \(f.date.formatted(.dateTime.month(.abbreviated).day()))")
                                .font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
                        }
                        .padding(.vertical, 14 * u).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .enter(.rise, i, u: u)
                }
                if files.count > shown {
                    Text("+\(files.count - shown) more · say a name or a number")
                        .font(.system(size: 22 * u)).foregroundStyle(Theme.polishBlue).padding(.top, 8 * u)
                }
            }
            .padding(.horizontal, 56 * u).padding(.vertical, 44 * u)
            .frame(width: 1000 * u, alignment: .leading)
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 1000 * u)
        }
        .frame(maxHeight: .infinity)
    }
}

// MARK: - import_photo: our camera

/// Where pages come from: the iPad camera now, the glasses' camera later (same
/// screen, same words; only the source changes).
@MainActor
protocol PhotoSource: AnyObject {
    var preview: AnyView { get }
    func start()
    func stop()
    func capture() async -> UIImage?
}

/// The iPad's back camera through AVCaptureSession.
@MainActor
final class DeviceCamera: NSObject, PhotoSource, AVCapturePhotoCaptureDelegate {
    private let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private var pending: CheckedContinuation<UIImage?, Never>?

    override init() {
        super.init()
        session.sessionPreset = .photo
        if let cam = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
           let input = try? AVCaptureDeviceInput(device: cam), session.canAddInput(input) {
            session.addInput(input)
        }
        if session.canAddOutput(output) { session.addOutput(output) }
    }

    var preview: AnyView { AnyView(CameraPreview(session: session)) }

    func start() {
        let s = session
        Task.detached {
            // asks once (NSCameraUsageDescription); denied: the preview stays dark
            guard await AVCaptureDevice.requestAccess(for: .video) else { return }
            if !s.isRunning { s.startRunning() }
        }
    }

    func stop() {
        let s = session
        Task.detached { if s.isRunning { s.stopRunning() } }
    }

    func capture() async -> UIImage? {
        guard session.isRunning, pending == nil else { return nil }
        return await withCheckedContinuation { c in
            pending = c
            output.capturePhoto(with: AVCapturePhotoSettings(), delegate: self)
        }
    }

    nonisolated func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto,
                                 error: Error?) {
        let img = photo.fileDataRepresentation().flatMap(UIImage.init(data:))
        Task { @MainActor in
            self.pending?.resume(returning: img)
            self.pending = nil
        }
    }
}

private struct CameraPreview: UIViewRepresentable {
    var session: AVCaptureSession
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.layer.videoGravity = .resizeAspectFill
        v.layer.session = session
        return v
    }
    func updateUIView(_ v: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        override var layer: AVCaptureVideoPreviewLayer { super.layer as! AVCaptureVideoPreviewLayer }
    }
}

struct CameraScreen: View {
    var source: PhotoSource
    var commands: ScreenCommands
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void
    /// The pages so far (the server counts them for "2 pages so far").
    var onPages: (Int) -> Void
    var onDone: ([Data]) -> Void
    /// The iOS photo picker is up (a live session stays open meanwhile).
    var onSystemSheet: (Bool) -> Void = { _ in }
    @State private var pages: [UIImage] = []
    @State private var choosing = false
    @State private var picks: [PhotosPickerItem] = []
    @State private var flash = false

    var body: some View {
        VStack(spacing: 28 * u) {
            ZStack(alignment: .topLeading) {
                source.preview
                    .clipShape(RoundedRectangle(cornerRadius: 44 * u, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 44 * u, style: .continuous)
                        .fill(.white.opacity(flash ? 0.6 : 0)))
                Text("Page \(pages.count + 1)").font(.system(size: 24 * u, weight: .semibold))
                    .foregroundStyle(.white).padding(.horizontal, 20 * u).padding(.vertical, 10 * u)
                    .background(Capsule().fill(.black.opacity(0.45))).padding(24 * u)
            }
            .frame(width: 1000 * u, height: 470 * u)
            // the pages taken: a strip of thumbnails
            HStack(spacing: 14 * u) {
                ForEach(Array(pages.enumerated()), id: \.offset) { i, img in
                    Image(uiImage: img).resizable().scaledToFill().frame(width: 64 * u, height: 84 * u)
                        .clipShape(RoundedRectangle(cornerRadius: 10 * u))
                        .overlay(alignment: .bottomTrailing) {
                            Text("\(i + 1)").font(.system(size: 14 * u, weight: .bold)).padding(4 * u)
                                .background(Circle().fill(.white))
                        }
                }
                Spacer(minLength: 0)
                Text(pages.isEmpty ? "No pages yet" : "\(pages.count) page\(pages.count == 1 ? "" : "s")")
                    .font(.system(size: 22 * u)).foregroundStyle(Theme.text2)
            }
            .frame(width: 1000 * u, height: 84 * u)
            SayRow(buttons: buttons, u: u * 0.8, perform: perform).frame(width: 1000 * u)
        }
        .frame(maxHeight: .infinity)
        .onAppear { source.start() }
        .onDisappear { source.stop() }
        .onChange(of: commands.gen) { _, _ in
            guard let c = commands.last else { return }
            Task { await apply(c.cmd) }
        }
        .onChange(of: pages.count) { _, n in onPages(n) }
        .photosPicker(isPresented: $choosing, selection: $picks, matching: .images)
        .onChange(of: picks) { _, items in
            Task { @MainActor in
                for item in items {
                    if let d = try? await item.loadTransferable(type: Data.self), let img = UIImage(data: d) {
                        pages.append(img)
                    }
                }
                picks = []
                onSystemSheet(false)
            }
        }
    }

    @MainActor
    private func apply(_ cmd: String) async {
        switch cmd {
        case "take": await take()
        case "retake":
            if !pages.isEmpty { pages.removeLast() }
            await take()
        case "one_more": break  // ready for the next page; the label shows its number
        case "done": if !pages.isEmpty { onDone(pages.compactMap { $0.jpegData(compressionQuality: 0.8) }) }
        case "choose":
            onSystemSheet(true)  // held until the picks arrive or the app is active again
            choosing = true
        default: break
        }
    }

    private func take() async {
        guard let img = await source.capture() else { return }
        withAnimation(Theme.Motion.ease(0.15)) { flash = true }
        pages.append(img)
        withAnimation(Theme.Motion.ease(0.35).delay(0.15)) { flash = false }
    }
}

// MARK: - web_source: a source page inside the app

struct WebSourceScreen: View {
    var url: URL
    var title: String
    var domain: String
    var commands: ScreenCommands
    var buttons: [ScreenButton]
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    var body: some View {
        VStack(spacing: 28 * u) {
            VStack(spacing: 0) {
                HStack(spacing: 12 * u) {
                    Image(systemName: "globe").foregroundStyle(Theme.polishBlue)
                    Text(title).foregroundStyle(Theme.text1).lineLimit(1)
                    Spacer(minLength: 0)
                    Text(domain).foregroundStyle(Theme.text2)
                }
                .font(.system(size: 22 * u))
                .padding(.horizontal, 40 * u).frame(height: 70 * u)
                .background(Theme.cardHead)
                WebPage(url: url, commands: commands)
            }
            .frame(width: 1100 * u, height: 560 * u)
            .clipShape(RoundedRectangle(cornerRadius: 44 * u, style: .continuous))
            .benchCard(radius: 44 * u)
            SayRow(buttons: buttons, u: u, perform: perform).frame(width: 1100 * u)
        }
        .frame(maxHeight: .infinity)
    }
}

private struct WebPage: UIViewRepresentable {
    var url: URL
    var commands: ScreenCommands

    func makeUIView(context: Context) -> WKWebView {
        let v = WKWebView()
        v.load(URLRequest(url: url))
        return v
    }

    func updateUIView(_ v: WKWebView, context: Context) {
        if v.url?.absoluteString != url.absoluteString, context.coordinator.loaded != url {
            context.coordinator.loaded = url
            v.load(URLRequest(url: url))
        }
        guard commands.gen != context.coordinator.gen else { return }
        context.coordinator.gen = commands.gen
        switch commands.last?.cmd {
        case "scroll_down": v.evaluateJavaScript("window.scrollBy({top: innerHeight * 0.8, behavior: 'smooth'})")
        case "scroll_up": v.evaluateJavaScript("window.scrollBy({top: -innerHeight * 0.8, behavior: 'smooth'})")
        default: break
        }
    }

    func makeCoordinator() -> Coord { Coord() }

    final class Coord {
        var gen = 0
        var loaded: URL?
    }
}
