#if DEBUG
import CoreGraphics
import Foundation
import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct PlateAimTests {
    @Test func entersOnceAfterHalfASecondAndLosesAfterTwo() {
        var s = PlateAimState()
        #expect(s.update(seen: true, at: 0) == nil)
        #expect(s.update(seen: true, at: 0.4) == nil)
        #expect(s.update(seen: true, at: 0.5) == .entered(first: true))
        #expect(s.update(seen: true, at: 0.75) == nil)
        #expect(s.update(seen: false, at: 1) == nil)
        #expect(s.update(seen: false, at: 2.75) == nil)
        #expect(s.update(seen: true, at: 3) == nil)  // back in time: never lost
        #expect(s.update(seen: false, at: 3.25) == nil)
        #expect(s.update(seen: false, at: 5.25) == .lost)
        #expect(s.update(seen: false, at: 9) == nil)
    }

    @Test func aMissRestartsTheEntryAndALaterEntryIsNotFirst() {
        var s = PlateAimState()
        _ = s.update(seen: true, at: 0)
        _ = s.update(seen: false, at: 0.4)
        #expect(s.update(seen: true, at: 0.6) == nil)
        #expect(s.update(seen: true, at: 1.1) == .entered(first: true))
        _ = s.update(seen: false, at: 2)
        #expect(s.update(seen: false, at: 4) == .lost)
        _ = s.update(seen: true, at: 5)
        #expect(s.update(seen: true, at: 5.5) == .entered(first: false))
    }

    @Test func outlineFollowsTheFillCrop() {
        let rect = CGRect(x: 0, y: 0, width: 150, height: 100)
        let image = CGSize(width: 90, height: 160)  // 9:16 filling 3:2: scale 1.667, sides fit, top cut
        let top = AimOutline.point(CGPoint(x: 0.5, y: 0), image: image, in: rect)
        let left = AimOutline.point(CGPoint(x: 0, y: 0.5), image: image, in: rect)
        #expect(abs(left.x) < 0.01 && abs(left.y - 50) < 0.01)
        #expect(abs(top.x - 75) < 0.01 && abs(top.y - (50 - 80 * 150.0 / 90)) < 0.01)
    }

    @Test func toneIsAShortWav() {
        let two = SamTone.wav([392, 330])
        #expect(two.prefix(4) == Data("RIFF".utf8))
        let pcm = two.count - 44
        #expect(abs(Double(pcm) / 2 / 44100 - 0.15) < 0.001)
    }
}
#endif
