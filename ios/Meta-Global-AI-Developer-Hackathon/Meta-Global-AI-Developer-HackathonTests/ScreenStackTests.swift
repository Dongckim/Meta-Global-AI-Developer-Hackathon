import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct ScreenStackTests {
    private enum S: CaseIterable { case splash, sync, main }

    private func inputs(_ s: ScreenStack<S>) -> [S] {
        S.allCases.filter { s.acceptsInput($0) }
    }

    @Test func exactlyOneScreenTakesInputThroughSwitches() {
        var s = ScreenStack(S.splash)
        #expect(inputs(s) == [.splash])
        s.show(.sync)
        #expect(inputs(s) == [.sync])
        #expect(s.leaving == .splash)
        #expect(s.isMounted(.splash) && s.isMounted(.sync))
        s.show(.main)
        #expect(inputs(s) == [.main])
        #expect(s.leaving == .sync)
        #expect(!s.isMounted(.splash))
    }

    @Test func staleLeaveIsIgnored() {
        var s = ScreenStack(S.splash)
        s.show(.sync)
        let first = s.generation
        s.show(.main)
        s.finishLeave(generation: first)
        #expect(s.leaving == .sync)
        s.finishLeave(generation: s.generation)
        #expect(s.leaving == nil)
        #expect(inputs(s) == [.main])
    }

    @Test func showingFrontIsNoOp() {
        var s = ScreenStack(S.main)
        s.show(.main)
        #expect(s.generation == 0)
        #expect(s.leaving == nil)
    }
}
