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

#endif
