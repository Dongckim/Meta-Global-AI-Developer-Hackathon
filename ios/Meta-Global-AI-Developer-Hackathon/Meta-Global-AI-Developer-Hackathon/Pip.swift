import Foundation
import Observation

// MARK: - JSON

/// Lenient JSON value for tool args (server sends plain JSON objects).
enum JSON: Decodable {
    case string(String), number(Double), bool(Bool), array([JSON]), object([String: JSON]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null } else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSON].self)) }
    }

    var string: String? { if case .string(let s) = self { s } else { nil } }
    var double: Double? {
        switch self {
        case .number(let n): n
        case .string(let s): Double(s)
        default: nil
        }
    }
    /// A bare string counts as a one-element list (models sometimes skip the array).
    var strings: [String]? {
        switch self {
        case .array(let a): a.compactMap(\.string)
        case .string(let s): [s]
        default: nil
        }
    }

    var compact: String {
        switch self {
        case .string(let s): "\"\(s)\""
        case .number(let n): n.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(n)) : String(n)
        case .bool(let b): b ? "true" : "false"
        case .array(let a): "[" + a.map(\.compact).joined(separator: ", ") + "]"
        case .object(let o): "{" + o.map { "\"\($0)\": \($1.compact)" }.sorted().joined(separator: ", ") + "}"
        case .null: "null"
        }
    }
}

func fmtMinutes(_ m: Double) -> String {
    m.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(m)) : String(m)
}

func msLine(_ ms: [String: Int]) -> String {
    ms.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: " · ") + " ms"
}

// MARK: - Plan

struct PlanStep: Decodable {
    var name: String
    var wells: [String]
    var tip: String?
}

struct PlanGroup: Decodable {
    var wells: [String]
    var n: Int
}

struct Plan: Decodable {
    var groups: [String: PlanGroup]
    var steps: [PlanStep]
    var currentStep: Int // 1-based
    enum CodingKeys: String, CodingKey {
        case groups, steps, currentStep = "current_step"
    }
}

struct PendingItem {
    let action: Action
    let question: String
    let detail: String?
    let fromCatch: Bool
}

struct PendingQuestion {
    var question: String
    var target: String?
}

/// "C2–C8" for contiguous same-row wells, comma list otherwise, nil when empty.
func wellRangeLabel(_ wells: [String]) -> String? {
    let rows = Array(Set(wells.map { $0.uppercased() }))
    guard !rows.isEmpty else { return nil }
    let parsed: [(String, Int)] = rows.compactMap { w in
        guard w.count >= 2, let col = Int(w.dropFirst()), (1...12).contains(col) else { return nil }
        let row = String(w.prefix(1))
        guard "ABCDEFGH".contains(row) else { return nil }
        return (row, col)
    }
    if parsed.count == rows.count, let first = parsed.first,
       parsed.allSatisfy({ $0.0 == first.0 }) {
        let lo = parsed.map(\.1).min()!, hi = parsed.map(\.1).max()!
        if hi - lo + 1 == parsed.count {
            return lo == hi ? rows[0] : "\(first.0)\(lo)–\(first.0)\(hi)"
        }
    }
    return rows.sorted().joined(separator: ", ")
}

/// All wells of the action(s) as one range label, else the first non-well identifier.
func targetOf(_ argList: [[String: JSON]]) -> String? {
    var wells: [String] = []
    for args in argList {
        if let w = args["well"]?.string { wells.append(w) }
        if let ws = args["wells"]?.strings { wells.append(contentsOf: ws) }
    }
    if !wells.isEmpty { return wellRangeLabel(wells) }
    for args in argList {
        if let t = args["target"]?.string { return t }
        if let s = args["step_id"]?.string { return s }
        if let l = args["label"]?.string { return l }
    }
    return nil
}

// MARK: - Action

/// One bench action. Mirrors the server's 10 tools.
enum Action {
    case markDone(wells: [String])
    case excludeWell(well: String, reason: String?)
    case recordRedo(wells: [String], note: String?)
    case addNote(target: String, text: String)
    case saveTip(step: String, text: String)
    case setTimer(label: String, minutes: Double, onDone: String?)
    case manageTimers(action: String, label: String?)
    case whereAmI
    case nextStep
    case show(view: String, target: String?)

    var needsConfirm: Bool {
        switch self {
        case .excludeWell, .recordRedo, .saveTip: true
        default: false
        }
    }

    var question: String? {
        switch self {
        case .excludeWell(let w, _): "Exclude \(w)?"
        case .recordRedo(let wells, _): "Redo \(wells.joined(separator: ", "))?"
        case .saveTip: "Save this tip?"
        default: nil
        }
    }

    var detail: String? {
        switch self {
        case .excludeWell(_, let r): r
        case .recordRedo(_, let n): n
        case .saveTip(let step, let text): "[\(step)] \(text)"
        default: nil
        }
    }

    static func fromTool(name: String, args: [String: JSON]) -> Action? {
        switch name {
        case "mark_done":
            guard let w = args["wells"]?.strings else { return nil }
            return .markDone(wells: w)
        case "exclude_well":
            guard let w = args["well"]?.string else { return nil }
            return .excludeWell(well: w, reason: args["reason"]?.string)
        case "record_redo":
            guard let w = args["wells"]?.strings else { return nil }
            return .recordRedo(wells: w, note: args["note"]?.string)
        case "add_note":
            guard let t = args["target"]?.string, let x = args["text"]?.string else { return nil }
            return .addNote(target: t, text: x)
        case "save_tip":
            guard let s = args["step_id"]?.string, let x = args["text"]?.string else { return nil }
            return .saveTip(step: s, text: x)
        case "set_timer":
            guard let l = args["label"]?.string, let m = args["minutes"]?.double else { return nil }
            return .setTimer(label: l, minutes: m, onDone: args["on_done"]?.string)
        case "manage_timers":
            guard let a = args["action"]?.string else { return nil }
            return .manageTimers(action: a, label: args["label"]?.string)
        case "where_am_i": return .whereAmI
        case "next_step": return .nextStep
        case "show":
            guard let v = args["view"]?.string else { return nil }
            return .show(view: v, target: args["target"]?.string)
        default: return nil
        }
    }
}

// MARK: - Lab state

@Observable
final class LabState {
    var planSteps: [PlanStep] = []
    var groups: [String: PlanGroup] = [:]
    var steps: [String] { planSteps.isEmpty ? ["Prep", "Dispense", "Incubate", "Measure", "Clean up"] : planSteps.map(\.name) }
    var stepIndex = 0
    var done: Set<String> = []
    var excluded: [String: String] = [:]
    var redo: [String: String] = [:]
    var notes: [(target: String, text: String)] = []
    var tips: [(step: String, text: String)] = []
    var timers: [(label: String, minutes: Double, onDone: String?)] = []
    var lastShown = ""
    var pending: [PendingItem] = []

    /// Reads samples/plan_demo.json from the bundle (once). No-op when absent.
    func loadPlan() {
        guard planSteps.isEmpty,
              let url = Bundle.main.url(forResource: "plan_demo", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let plan = try? JSONDecoder().decode(Plan.self, from: data)
        else { return }
        load(plan: plan)
    }

    func load(plan: Plan) {
        guard !plan.steps.isEmpty else { return }
        planSteps = plan.steps
        groups = plan.groups
        stepIndex = min(max(plan.currentStep - 1, 0), plan.steps.count - 1)
    }

    var pendingQuestion: PendingQuestion?
    var answerTarget: String?

    var serverState: [String: Any] {
        var s: [String: Any] = ["step": stepIndex, "total": steps.count, "step_name": steps[stepIndex],
                                "done": done.sorted(), "excluded": excluded.keys.sorted(),
                                "timers": timers.map { ["label": $0.label, "minutes": $0.minutes] }]
        if let pq = pendingQuestion {
            s["pending_question"] = ["question": pq.question, "target": pq.target ?? ""]
        }
        return s
    }

    /// State for the next server call. Attaches a pending question once, then clears it,
    /// keeping its target for this round's timer labels.
    func takeState() -> [String: Any] {
        let s = serverState
        answerTarget = pendingQuestion?.target
        pendingQuestion = nil
        return s
    }

    /// Remembers a model question (with its target) so the next utterance answers it.
    func noteQuestion(reply: String, target: String?) {
        let q = reply.trimmingCharacters(in: .whitespacesAndNewlines)
        guard q.hasSuffix("?"), pending.isEmpty else { return }
        pendingQuestion = PendingQuestion(question: reply, target: target)
    }

    /// Per-group usable-done counts: done wells minus excluded ones, vs required n.
    func groupProgress() -> [(name: String, have: Int, need: Int)] {
        let usable = done.subtracting(excluded.keys)
        return groups.sorted { $0.key < $1.key }.map {
            (name: $0.key, have: usable.intersection($0.value.wells).count, need: $0.value.n)
        }
    }

    /// Applies immediately, or queues when the action needs confirmation. Returns log messages.
    func enqueue(_ actions: [Action]) -> [String] {
        var out: [String] = []
        for a in actions {
            if case .markDone(let wells) = a, !unplanned(wells).isEmpty {
                let bad = unplanned(wells)
                let q = bad.count == 1
                    ? "\(bad[0]) is not in the step \(stepIndex + 1) plan. Record it anyway?"
                    : "\(bad.joined(separator: ", ")) are not in the step \(stepIndex + 1) plan. Record them anyway?"
                pending.append(PendingItem(action: a, question: q, detail: nil, fromCatch: true))
            } else if a.needsConfirm {
                pending.append(PendingItem(action: a, question: a.question ?? "", detail: a.detail, fromCatch: false))
            } else {
                out.append(apply(a))
            }
        }
        return out
    }

    /// Wells missing from the current step's plan. Decided here in Swift, never by the model.
    func unplanned(_ wells: [String]) -> [String] {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return [] }
        let planned = Set(planSteps[stepIndex].wells)
        return wells.filter { !planned.contains($0) }
    }

    /// Answers the first pending card. Returns the result message, or nil when declined.
    func confirmFirst(_ ok: Bool) -> String? {
        guard !pending.isEmpty else { return nil }
        let first = pending.removeFirst()
        return ok ? apply(first.action) : nil
    }

    func apply(_ action: Action) -> String {
        switch action {
        case .markDone(let wells):
            done.formUnion(wells)
            return "✓ \(wells.joined(separator: ", ")) done"
        case .excludeWell(let well, let reason):
            excluded[well] = reason ?? ""
            return reason.map { "\(well) excluded (\(trimmed($0, 40)))" } ?? "\(well) excluded"
        case .recordRedo(let wells, let note):
            for w in wells { redo[w] = note ?? "" }
            return note.map { "\(wells.joined(separator: ", ")) marked for redo (\(trimmed($0, 40)))" }
                ?? "\(wells.joined(separator: ", ")) marked for redo"
        case .addNote(let target, let text):
            notes.append((target, text))
            return "Note saved (\(target))"
        case .saveTip(let step, let text):
            tips.append((step, text))
            return "Tip saved (\(step))"
        case .setTimer(_, let minutes, let onDone):
            let finalLabel = answerTarget ?? steps[stepIndex]
            timers.append((finalLabel, minutes, onDone))
            return "Timer started: \(finalLabel), \(fmtMinutes(minutes)) min"
        case .manageTimers(let action, let label):
            return manageTimers(action: action, label: label)
        case .whereAmI:
            return "Step \(stepIndex + 1) of \(steps.count), \(steps[stepIndex]). \(done.count) wells done."
        case .nextStep:
            if stepIndex >= steps.count - 1 { return "Already at the last step" }
            stepIndex += 1
            return "Next: \(steps[stepIndex])"
        case .show(let view, let target):
            lastShown = target.map { "\(view) \($0)" } ?? view
            return "Viewing: \(lastShown)"
        }
    }

    private func manageTimers(action: String, label: String?) -> String {
        switch action.lowercased() {
        case "list":
            return timers.isEmpty ? "No running timers"
                : "Timers: " + timers.map { "\($0.label) \(fmtMinutes($0.minutes)) min" }.joined(separator: ", ")
        case "cancel":
            guard let label else { return "Which timer should I cancel?" }
            let before = timers.count
            timers.removeAll { $0.label == label }
            return timers.count < before ? "Timer canceled: \(label)" : "No such timer: \(label)"
        case "extend":
            guard let label else { return "Which timer should I extend?" }
            guard let i = timers.firstIndex(where: { $0.label == label }) else { return "No such timer: \(label)" }
            timers[i].minutes += 5
            return "+5 min: \(label) (\(fmtMinutes(timers[i].minutes)) min)"
        default: return "Unknown timer action: \(action)"
        }
    }

    private func trimmed(_ s: String, _ n: Int) -> String {
        s.count > n ? String(s.prefix(n)) + "…" : s
    }
}

// MARK: - Local text rules

extension Action {
    /// Short in-app rules. Returns nil when the text should go to /orchestrate.
    static func rule(_ text: String) -> Action? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let low = t.lowercased()
        if low.contains("next") { return .nextStep }
        if low.contains("where") { return .whereAmI }
        if low.contains("show") || low.contains("plate") {
            return .show(view: "plate", target: findWells(in: t).first)
        }
        if low.contains("timer"), let m = findMinutes(in: t) {
            return .setTimer(label: "Timer", minutes: m, onDone: nil)
        }
        let wells = findWells(in: t)
        if !wells.isEmpty {
            if low.contains("exclude") { return .excludeWell(well: wells[0], reason: nil) }
            if low.contains("redo") { return .recordRedo(wells: wells, note: nil) }
            if low.contains("done") { return .markDone(wells: wells) }
        }
        return nil
    }

    private static func findWells(in text: String) -> [String] {
        guard let re = try? NSRegularExpression(pattern: "(?i)\\b([A-H])(1[0-2]|[1-9])\\b") else { return [] }
        let range = NSRange(text.startIndex..., in: text)
        var out: [String] = []
        for m in re.matches(in: text, range: range) {
            let w = String(text[Range(m.range, in: text)!]).uppercased()
            if !out.contains(w) { out.append(w) }
        }
        return out
    }

    private static func findMinutes(in text: String) -> Double? {
        guard let re = try? NSRegularExpression(pattern: "(\\d+(?:\\.\\d+)?)\\s*min") else { return nil }
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.matches(in: text, range: range).first,
              let r = Range(m.range(at: 1), in: text) else { return nil }
        return Double(text[r])
    }
}
