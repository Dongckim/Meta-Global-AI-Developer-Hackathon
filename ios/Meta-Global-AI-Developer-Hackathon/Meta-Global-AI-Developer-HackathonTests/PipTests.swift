import Testing
@testable import Meta_Global_AI_Developer_Hackathon

struct PipTests {
    static func plan() -> Plan {
        Plan(groups: ["drug_A": PlanGroup(wells: ["C2", "C3"], n: 2)], steps: [
            PlanStep(name: "s1", wells: ["B2"], tip: nil),
            PlanStep(name: "s2", wells: ["C2", "C3"], tip: nil),
        ], currentStep: 2)
    }

    @Test @MainActor func plannedWellAppliesDirectly() {
        let lab = LabState()
        lab.load(plan: Self.plan())
        #expect(lab.enqueue([.markDone(wells: ["C3"])]) == ["✓ C3 done"])
        #expect(lab.pending.isEmpty)
        #expect(lab.done == ["C3"])
    }

    @Test @MainActor func unplannedWellAsksFirst() {
        let lab = LabState()
        lab.load(plan: Self.plan())
        #expect(lab.enqueue([.markDone(wells: ["A1"])]) == [])
        #expect(lab.pending.count == 1)
        #expect(lab.pending.first?.question == "A1 is not in the step 2 plan. Record it anyway?")
        #expect(lab.done.isEmpty)
        #expect(lab.confirmFirst(true) == "✓ A1 done")
        #expect(lab.done == ["A1"])
    }

    @Test @MainActor func noPlanAppliesDirectly() {
        let lab = LabState()
        #expect(lab.enqueue([.markDone(wells: ["A1"])]) == ["✓ A1 done"])
        #expect(lab.pending.isEmpty)
    }

    @Test @MainActor func followUpQuestionRememberedOnce() {
        let lab = LabState()
        lab.noteQuestion(reply: "Which well?", target: "C8")
        let st = lab.takeState()
        #expect((st["pending_question"] as? [String: String])?["target"] == "C8")
        #expect(lab.pendingQuestion == nil)
    }

    @Test @MainActor func pendingTargetKeepsFullRange() {
        let args: [[String: JSON]] = [["wells": .array([.string("C2"), .string("C3")])]]
        #expect(targetOf(args) == "C2–C3")
    }

    @Test @MainActor func timerIgnoresModelLabel() {
        let lab = LabState()
        lab.load(plan: Self.plan())
        _ = lab.enqueue([.markDone(wells: ["C2", "C3"])])
        lab.noteQuestion(reply: "How many minutes for the timer?", target: "C2–C3")
        _ = lab.takeState()
        #expect(lab.apply(.setTimer(label: "model label", minutes: 30, onDone: nil))
            == "Timer started: C2–C3, 30 min")
        #expect(lab.timers.count == 1)
    }

    @Test @MainActor func timerFallsBackToStepName() {
        let lab = LabState()
        lab.load(plan: Self.plan())
        _ = lab.takeState()
        #expect(lab.apply(.setTimer(label: "model label", minutes: 5, onDone: nil))
            == "Timer started: s2, 5 min")
    }

    @Test @MainActor func msLineSortsKeys() {
        #expect(msLine(["spark": 1340, "asr": 812, "total": 2201]) == "asr 812 · spark 1340 · total 2201 ms")
    }
}
