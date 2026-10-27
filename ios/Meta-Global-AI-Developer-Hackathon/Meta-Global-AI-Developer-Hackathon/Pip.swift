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

func fmtClock(_ s: TimeInterval) -> String {
    "\(Int(s) / 60):\(String(format: "%02d", Int(s) % 60))"
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
    var currentStep: Int?
}

// MARK: - Record snapshot (server is the single truth; the app renders this)

struct MemoEdit: Decodable, Equatable {
    var text: String
    var ts: Double
}

struct Memo: Decodable, Equatable, Identifiable {
    var id: String
    var target: String
    var text: String
    var raw: String?
    var ts: Double
    var history: [MemoEdit]
}

struct SavedTip: Decodable, Equatable {
    var step: String
    var text: String
}

struct TimerEntry: Decodable, Equatable, Identifiable {
    var id: String
    var label: String
    var minutes: Double
    var onDone: String?
    var started: Date
    var step: String
    var wells: String

    enum CodingKeys: String, CodingKey {
        case id, label, minutes, onDone = "on_done", started = "started_at", step, wells
    }

    init(id: String, label: String, minutes: Double, onDone: String? = nil,
         started: Date = Date(), step: String = "", wells: String = "") {
        self.id = id
        self.label = label
        self.minutes = minutes
        self.onDone = onDone
        self.started = started
        self.step = step
        self.wells = wells
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = try c.decode(String.self, forKey: .label)
        minutes = try c.decode(Double.self, forKey: .minutes)
        onDone = try c.decodeIfPresent(String.self, forKey: .onDone)
        started = Date(timeIntervalSince1970: try c.decode(Double.self, forKey: .started))
        step = try c.decode(String.self, forKey: .step)
        wells = try c.decode(String.self, forKey: .wells)
    }
}

func isOver(_ t: TimerEntry, now: Date) -> Bool {
    now.timeIntervalSince(t.started) >= t.minutes * 60
}

struct Stamp: Decodable {
    var source: String
    var ts: Double
}

struct ServerCard: Decodable, Equatable, Identifiable {
    var id: String
    var kind: String
    var question: String
    var detail: String?
    var ts: Double
}

struct RecordSnapshot: Decodable {
    var version: Int
    var plan: Plan
    var stepIndex: Int
    var done: [String]
    var excluded: [String: String]
    var redo: [String: String]
    var notes: [Memo]
    var tips: [SavedTip]
    var timers: [TimerEntry]
    var stamps: [String: Stamp]
    var cards: [ServerCard]

    enum CodingKeys: String, CodingKey {
        case version, plan, stepIndex = "step_index", done, excluded, redo,
             notes, tips, timers, stamps, cards
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        plan = try c.decode(Plan.self, forKey: .plan)
        stepIndex = try c.decode(Int.self, forKey: .stepIndex)
        done = try c.decode([String].self, forKey: .done)
        excluded = try c.decode([String: String].self, forKey: .excluded)
        redo = try c.decode([String: String].self, forKey: .redo)
        notes = try c.decode([Memo].self, forKey: .notes)
        tips = try c.decode([SavedTip].self, forKey: .tips)
        timers = try c.decode([TimerEntry].self, forKey: .timers)
        stamps = try c.decode([String: Stamp].self, forKey: .stamps)
        cards = try c.decode([String: ServerCard].self, forKey: .cards).values.sorted { $0.ts > $1.ts }
    }
}

let plateRows = ["A", "B", "C", "D", "E", "F", "G", "H"]

/// show() targets: a letter is a row, a number is a column, both is one well.
func parseShowTarget(_ raw: String?) -> Set<String> {
    guard var t = raw?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(), !t.isEmpty else { return [] }
    for p in ["COLUMN ", "COL ", "ROW ", "WELL "] where t.hasPrefix(p) {
        t = String(t.dropFirst(p.count)).trimmingCharacters(in: .whitespaces)
        break
    }
    if t.count >= 2, let r = t.first, plateRows.contains(String(r)),
       let c = Int(t.dropFirst()), (1...12).contains(c) { return ["\(r)\(c)"] }
    if t.count == 1, plateRows.contains(t) { return Set((1...12).map { "\(t)\($0)" }) }
    if let c = Int(t), (1...12).contains(c) { return Set(plateRows.map { "\($0)\(c)" }) }
    return []
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

// MARK: - Lab state (render model; the server owns the record)

@Observable
final class LabState {
    var version = -1
    var planSteps: [PlanStep] = []
    var groups: [String: PlanGroup] = [:]
    var steps: [String] = ["Prep", "Dispense", "Incubate", "Measure", "Clean up"]
    var stepIndex = 0
    var done: Set<String> = []
    var excluded: [String: String] = [:]
    var redo: [String: String] = [:]
    var notes: [Memo] = []
    var timers: [TimerEntry] = []
    var cards: [ServerCard] = []
    var lastShown = ""
    /// Per-well provenance for the well card: source and last record time.
    var wellSource: [String: String] = [:]
    var wellTime: [String: Date] = [:]
    /// Wells lit by show(), cleared 2 s after the last call.
    var flashing: Set<String> = []
    private var flashGen = 0

    /// Replace everything with a server snapshot. Older versions are ignored.
    func sync(_ snap: RecordSnapshot) {
        guard snap.version > version else { return }
        version = snap.version
        planSteps = snap.plan.steps
        groups = snap.plan.groups
        if !snap.plan.steps.isEmpty { steps = snap.plan.steps.map(\.name) }
        stepIndex = min(max(snap.stepIndex, 0), steps.count - 1)
        done = Set(snap.done)
        excluded = snap.excluded
        redo = snap.redo
        notes = snap.notes
        timers = snap.timers
        cards = snap.cards
        wellSource = snap.stamps.mapValues(\.source)
        wellTime = snap.stamps.mapValues { Date(timeIntervalSince1970: $0.ts) }
    }

    /// Per-group usable-done counts: done wells minus excluded ones, vs required n.
    func groupProgress() -> [(name: String, have: Int, need: Int)] {
        let usable = done.subtracting(excluded.keys)
        return groups.sorted { $0.key < $1.key }.map {
            (name: $0.key, have: usable.intersection($0.value.wells).count, need: $0.value.n)
        }
    }

    func groupOf(_ well: String) -> String? {
        groups.first { $0.value.wells.contains(well) }?.key
    }

    var currentTip: String? {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return nil }
        return planSteps[stepIndex].tip
    }

    var currentRange: String? {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return nil }
        return wellRangeLabel(planSteps[stepIndex].wells)
    }

    var currentPlanned: Set<String> {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return [] }
        return Set(planSteps[stepIndex].wells)
    }

    func hasNote(_ well: String) -> Bool {
        notes.contains { $0.target == well }
    }

    func memos(for well: String) -> [Memo] {
        notes.filter { $0.target == well }
    }

    /// Timers still shown: active ones plus done ones younger than a minute.
    func visibleTimers(now: Date) -> [TimerEntry] {
        timers.filter { now.timeIntervalSince($0.started) < $0.minutes * 60 + 60 }
    }

    func flash(_ wells: Set<String>) {
        flashing = wells
        flashGen += 1
        let g = flashGen
        // Fires on the caller's (main) runloop; newer flashes win via the generation.
        Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in
            guard let self, self.flashGen == g else { return }
            self.flashing = []
        }
    }
}

struct ScreenDef: Decodable {
    var id: String
    var commands: [String]
}

/// Screen registry shared with the server (same screens.json). Falls back to
/// the built-in three when the bundle copy is missing or undecodable.
func loadScreens() -> [ScreenDef] {
    if let url = Bundle.main.url(forResource: "screens", withExtension: "json"),
       let data = try? Data(contentsOf: url),
       let obj = try? JSONDecoder().decode([String: [ScreenDef]].self, from: data),
       let list = obj["screens"], !list.isEmpty {
        return list
    }
    return [
        ScreenDef(id: "main", commands: ["mark C2 done", "start a 5 minute timer", "show C5"]),
        ScreenDef(id: "well_card", commands: ["note bubbles", "exclude this well", "back"]),
        ScreenDef(id: "robot_preview", commands: ["make the robot protocol", "back"]),
    ]
}
