//  Sound feedback for the SAM well test: is the plate in the glasses' view
//  (Apple Vision rectangles, on the device), and short tones. Aiming only: the
//  well judging stays SAM's. Test only, nothing in the product flow calls this.

#if DEBUG
import AVFoundation
import os
import SwiftUI

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
