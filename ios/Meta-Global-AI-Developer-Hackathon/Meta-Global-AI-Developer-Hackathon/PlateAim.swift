//  Sound feedback for the SAM well test: is the plate in the glasses' view
//  (Apple Vision rectangles, on the device), and short tones. Aiming only: the
//  well judging stays SAM's. Test only, nothing in the product flow calls this.

#if DEBUG
import AVFoundation
import os
import SwiftUI
import Vision

/// Plate seen / gone from the detector's yes/no over time. Entered after 0.5 s
/// in a row, lost after 2 s in a row; `first` is the page's first entry.
nonisolated struct PlateAimState {
    enum Event: Equatable { case entered(first: Bool), lost }

    private(set) var inView = false
    private var announced = false
    private var seenSince: Double?
    private var goneSince: Double?

    mutating func update(seen: Bool, at t: Double) -> Event? {
        if seen {
            goneSince = nil
            guard !inView else { return nil }
            let since = seenSince ?? t
            seenSince = since
            guard t - since >= 0.5 else { return nil }
            inView = true
            defer { announced = true }
            return .entered(first: !announced)
        }
        seenSince = nil
        guard inView else { return nil }
        let since = goneSince ?? t
        goneSince = since
        guard t - since >= 2 else { return nil }
        inView = false
        goneSince = nil
        return .lost
    }
}

/// Looks for a plate-shaped rectangle in the stream, about 4 times a second,
/// off the stream's thread. `found` gets the corners (0-1, top left origin), or nil.
final class PlateFinder: @unchecked Sendable {
    private let queue = DispatchQueue(label: "samwell.aim", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false
    private var last = 0.0
    private let found: @Sendable ([CGPoint]?) -> Void

    init(found: @escaping @Sendable ([CGPoint]?) -> Void) { self.found = found }

    func frame(_ sb: CMSampleBuffer) {
        let now = CACurrentMediaTime()
        let go = lock.withLock {
            guard !busy, now - last >= 0.25 else { return false }
            busy = true
            last = now
            return true
        }
        guard go, let pb = CMSampleBufferGetImageBuffer(sb) else {
            if go { lock.withLock { busy = false } }
            return
        }
        nonisolated(unsafe) let buffer = pb
        queue.async { [self] in
            let quad = Self.detect(buffer)
            lock.withLock { busy = false }
            found(quad)
        }
    }

    private static func detect(_ pb: CVPixelBuffer) -> [CGPoint]? {
        let request = VNDetectRectanglesRequest()
        // a plate is 2:3; tilted it reads narrower, a little wider from the side
        request.minimumAspectRatio = 0.45
        request.maximumAspectRatio = 0.9
        request.minimumSize = 0.15
        request.quadratureTolerance = 30
        request.minimumConfidence = 0.5
        request.maximumObservations = 0
        do {
            try VNImageRequestHandler(cvPixelBuffer: pb, orientation: .up).perform([request])
        } catch {
            Log.view.error("[samwell] rectangles failed: \(error.localizedDescription, privacy: .public)")
            return nil
        }
        // shortcut: the largest rectangle, a sheet of paper can pass for a plate; aiming only, SAM judges
        let best = request.results?.max { area($0) < area($1) }
        return best.map { o in [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft].map { CGPoint(x: $0.x, y: 1 - $0.y) } }
    }

    private static func area(_ o: VNRectangleObservation) -> Double {
        let q = [o.topLeft, o.topRight, o.bottomRight, o.bottomLeft]
        return abs(zip(q, q[1...] + q[..<1]).reduce(0) { $0 + $1.0.x * $1.1.y - $1.1.x * $1.0.y }) / 2
    }
}

/// The rectangle's corners on a preview that shows an `image`-sized picture
/// scaled to fill the rect, centered.
struct AimOutline: Shape {
    var quad: [CGPoint]
    var image: CGSize

    static func point(_ p: CGPoint, image: CGSize, in rect: CGRect) -> CGPoint {
        let s = max(rect.width / image.width, rect.height / image.height)
        return CGPoint(x: rect.midX + (p.x - 0.5) * image.width * s, y: rect.midY + (p.y - 0.5) * image.height * s)
    }

    func path(in rect: CGRect) -> Path {
        Path { $0.addLines(quad.map { Self.point($0, image: image, in: rect) }); $0.closeSubpath() }
    }
}

/// Short tones, made here (no sound files). They play through the audio
/// session's current route at under TTS level, and the mic keeps listening.
@MainActor
enum SamTone {
    case rising, falling, lowDing, highDing, lowTwo

    private static let volume: Float = 0.35
    private static var player: AVAudioPlayer?

    var notes: [Double] {
        switch self {
        case .rising: [660, 880]
        case .falling: [880, 660]
        case .lowDing: [523]
        case .highDing: [1319]
        case .lowTwo: [392, 330]
        }
    }

    func play() {
        do {
            let p = try AVAudioPlayer(data: Self.wav(notes))
            p.volume = Self.volume
            p.play()
            Self.player = p
            Log.audio.info("[samwell] tone \(String(describing: self), privacy: .public) \(Log.route(), privacy: .public)")
        } catch {
            Log.audio.error("[samwell] tone failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// 0.15 s of 16-bit mono WAV, the notes one after the other, each faded so it doesn't click.
    nonisolated static func wav(_ notes: [Double], seconds: Double = 0.15, rate: Int = 44100) -> Data {
        let each = Int(seconds * Double(rate)) / notes.count
        var pcm = Data(capacity: each * notes.count * 2)
        for f in notes {
            for i in 0..<each {
                let fade = sin(.pi * Double(i) / Double(each))
                let v = Int16(sin(2 * .pi * f * Double(i) / Double(rate)) * fade * 28000)
                withUnsafeBytes(of: v.littleEndian) { pcm.append(contentsOf: $0) }
            }
        }
        func le<T: FixedWidthInteger>(_ v: T) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
        var h = Data("RIFF".utf8) + le(UInt32(36 + pcm.count)) + Data("WAVEfmt ".utf8)
        h += le(UInt32(16)) + le(UInt16(1)) + le(UInt16(1)) + le(UInt32(rate)) + le(UInt32(rate * 2))
        h += le(UInt16(2)) + le(UInt16(16)) + Data("data".utf8) + le(UInt32(pcm.count))
        return h + pcm
    }
}
#endif
