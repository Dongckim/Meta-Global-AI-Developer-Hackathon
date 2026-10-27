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
        case .excludeWell(let w, _): "\(w) 제외할까요?"
        case .recordRedo(let wells, _): "\(wells.joined(separator: ", ")) 다시 할까요?"
        case .saveTip: "팁을 저장할까요?"
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
    let steps = ["준비", "분주", "배양", "측정", "정리"]
    var stepIndex = 0
    var done: Set<String> = []
    var excluded: [String: String] = [:]
    var redo: [String: String] = [:]
    var notes: [(target: String, text: String)] = []
    var tips: [(step: String, text: String)] = []
    var timers: [(label: String, minutes: Double, onDone: String?)] = []
    var lastShown = ""
    var pending: [Action] = []

    var serverState: [String: Any] {
        ["step": stepIndex, "done": done.sorted(), "excluded": excluded.keys.sorted()]
    }

    /// Applies immediately, or queues when the action needs confirmation. Returns log messages.
    func enqueue(_ actions: [Action]) -> [String] {
        var out: [String] = []
        for a in actions {
            if a.needsConfirm { pending.append(a) } else { out.append(apply(a)) }
        }
        return out
    }

    /// Answers the first pending card. Returns the result message, or nil when declined.
    func confirmFirst(_ ok: Bool) -> String? {
        guard !pending.isEmpty else { return nil }
        let first = pending.removeFirst()
        return ok ? apply(first) : nil
    }

    func apply(_ action: Action) -> String {
        switch action {
        case .markDone(let wells):
            done.formUnion(wells)
            return "✓ \(wells.joined(separator: ", ")) 완료"
        case .excludeWell(let well, let reason):
            excluded[well] = reason ?? ""
            return reason.map { "\(well) 제외함 (\(trimmed($0, 40)))" } ?? "\(well) 제외함"
        case .recordRedo(let wells, let note):
            for w in wells { redo[w] = note ?? "" }
            return note.map { "\(wells.joined(separator: ", ")) 다시 하기 (\(trimmed($0, 40)))" }
                ?? "\(wells.joined(separator: ", ")) 다시 하기 기록"
        case .addNote(let target, let text):
            notes.append((target, text))
            return "메모 저장 (\(target))"
        case .saveTip(let step, let text):
            tips.append((step, text))
            return "팁 저장 (\(step))"
        case .setTimer(let label, let minutes, let onDone):
            timers.append((label, minutes, onDone))
            return "타이머 시작: \(label) \(fmtMinutes(minutes))분"
        case .manageTimers(let action, let label):
            return manageTimers(action: action, label: label)
        case .whereAmI:
            return "지금 \(stepIndex + 1)/\(steps.count)단계 \(steps[stepIndex]) · 완료 \(done.count)개"
        case .nextStep:
            if stepIndex >= steps.count - 1 { return "마지막 단계야" }
            stepIndex += 1
            return "다음: \(steps[stepIndex])"
        case .show(let view, let target):
            lastShown = target.map { "\(view) \($0)" } ?? view
            return "화면: \(lastShown)"
        }
    }

    private func manageTimers(action: String, label: String?) -> String {
        switch action.lowercased() {
        case "list":
            return timers.isEmpty ? "실행 중인 타이머 없음"
                : "타이머: " + timers.map { "\($0.label) \(fmtMinutes($0.minutes))분" }.joined(separator: ", ")
        case "cancel":
            guard let label else { return "어떤 타이머를 취소할까?" }
            let before = timers.count
            timers.removeAll { $0.label == label }
            return timers.count < before ? "타이머 취소: \(label)" : "그런 타이머 없음: \(label)"
        case "extend":
            guard let label else { return "어떤 타이머를 늘릴까?" }
            guard let i = timers.firstIndex(where: { $0.label == label }) else { return "그런 타이머 없음: \(label)" }
            timers[i].minutes += 5
            return "+\(5)분: \(label) (\(fmtMinutes(timers[i].minutes))분)"
        default: return "모르는 타이머 동작: \(action)"
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
        if t.contains("다음") { return .nextStep }
        if t.contains("어디") { return .whereAmI }
        if t.contains("보여") || low.contains("plate") {
            return .show(view: "plate", target: findWells(in: t).first)
        }
        if t.contains("타이머"), let m = findMinutes(in: t) {
            return .setTimer(label: "타이머", minutes: m, onDone: nil)
        }
        let wells = findWells(in: t)
        if !wells.isEmpty {
            if t.contains("제외") || t.contains("빼") { return .excludeWell(well: wells[0], reason: nil) }
            if t.contains("다시") { return .recordRedo(wells: wells, note: nil) }
            if t.contains("끝") || t.contains("완료") || low.contains("done") { return .markDone(wells: wells) }
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
        guard let re = try? NSRegularExpression(pattern: "(\\d+(?:\\.\\d+)?)\\s*분") else { return nil }
        let range = NSRange(text.startIndex..., in: text)
        guard let m = re.matches(in: text, range: range).first,
              let r = Range(m.range(at: 1), in: text) else { return nil }
        return Double(text[r])
    }
}
