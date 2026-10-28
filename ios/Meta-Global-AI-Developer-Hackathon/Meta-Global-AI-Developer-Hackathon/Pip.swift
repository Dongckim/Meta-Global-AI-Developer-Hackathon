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

func fmtNum(_ v: Double) -> String {
    v.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(v)) : String(v)
}

func msLine(_ ms: [String: Int]) -> String {
    ms.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: " · ") + " ms"
}

// MARK: - Plan

struct PlanPipette: Decodable {
    var liquid: String
    var volume: Double?
    var unit: String?
    var wells: [String]
    var targetLabel: String

    enum CodingKeys: String, CodingKey {
        case liquid, volume, unit, wells, targetLabel = "target_label"
    }
}

struct PlanStep: Decodable {
    var kind: String
    var text: String
    var minutes: Double?
    var tempC: Double?
    var tip: String?
    var caution: String?
    var pipette: PlanPipette?
    var wells: [String]
    var quote: String?
    var quotePage: Int?

    enum CodingKeys: String, CodingKey {
        case kind, text, minutes, tempC = "temp_c", tip, caution, pipette, wells,
             quote, quotePage = "quote_page"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        text = try c.decode(String.self, forKey: .text)
        minutes = try c.decodeIfPresent(Double.self, forKey: .minutes)
        tempC = try c.decodeIfPresent(Double.self, forKey: .tempC)
        tip = try c.decodeIfPresent(String.self, forKey: .tip)
        caution = try c.decodeIfPresent(String.self, forKey: .caution)
        pipette = try c.decodeIfPresent(PlanPipette.self, forKey: .pipette)
        wells = try c.decodeIfPresent([String].self, forKey: .wells) ?? []
        quote = try c.decodeIfPresent(String.self, forKey: .quote)
        quotePage = try c.decodeIfPresent(Int.self, forKey: .quotePage)
    }
}

struct PlanCondition: Decodable {
    var label: String
    var treatments: [String]
    var replicates: Int
    var wells: [String]

    enum CodingKeys: String, CodingKey {
        case label, treatments, replicates, wells
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        treatments = try c.decodeIfPresent([String].self, forKey: .treatments) ?? []
        wells = try c.decodeIfPresent([String].self, forKey: .wells) ?? []
        replicates = try c.decodeIfPresent(Int.self, forKey: .replicates) ?? wells.count
    }
}

struct Plan: Decodable {
    var conditions: [String: PlanCondition]
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

struct HistoryEntry: Decodable, Identifiable {
    var id: Double { ts }
    var route: String?
    var action: String
    var wells: [String]
    var ts: Double
}

struct ServerCard: Decodable, Equatable, Identifiable {
    var id: String
    var kind: String
    var question: String
    var detail: String?
    var ts: Double
}

// MARK: - Server view

struct ViewTarget: Decodable, Equatable {
    var screen: String = ""
    var target: String = ""
}

struct ViewSheet: Decodable, Equatable {
    var name: String = ""
    var target: String = ""
}

struct ViewState: Decodable, Equatable {
    var current = ViewTarget()
    var stack: [ViewTarget] = []
    var tab = ""
    var sheet = ViewSheet()

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        current = try c.decodeIfPresent(ViewTarget.self, forKey: .current) ?? ViewTarget()
        stack = try c.decodeIfPresent([ViewTarget].self, forKey: .stack) ?? []
        tab = try c.decodeIfPresent(String.self, forKey: .tab) ?? ""
        sheet = try c.decodeIfPresent(ViewSheet.self, forKey: .sheet) ?? ViewSheet()
    }

    private enum CodingKeys: String, CodingKey {
        case current, stack, tab, sheet
    }
}

// MARK: - Library + runs

struct RunBlock: Decodable, Equatable {
    var runId: String
    var protocolId: String?
    var protocolName: String?
    var startedAt: Double

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", protocolId = "protocol_id",
             protocolName = "protocol_name", startedAt = "started_at"
    }
}

struct ProtocolSummary: Decodable, Identifiable {
    var id: String
    var name: String
    var sourceFilename: String?
    var importedAt: Double?
    var conditions: Int
    var steps: Int

    enum CodingKeys: String, CodingKey {
        case id, name, sourceFilename = "source_filename", importedAt = "imported_at",
             conditions, steps
    }
}

struct ProtocolDoc: Decodable {
    var id: String
    var name: String
    var sourceFilename: String?
    var importedAt: Double?
    var plan: Plan

    enum CodingKeys: String, CodingKey {
        case id, name, sourceFilename = "source_filename", importedAt = "imported_at", plan
    }
}

struct RunCondCount: Decodable {
    var key: String
    var label: String
    var have: Int
    var need: Int?
}

struct RunCounts: Decodable {
    var conditions: [RunCondCount]
    var excluded: Int
    var notes: Int
    var timers: Int
}

struct RunSummary: Decodable, Identifiable {
    var runId: String
    var protocolId: String?
    var protocolName: String?
    var startedAt: Double?
    var endedAt: Double?
    var status: String
    var summary: RunCounts?

    var id: String { runId }

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", protocolId = "protocol_id", protocolName = "protocol_name",
             startedAt = "started_at", endedAt = "ended_at", status, summary
    }
}

struct RunDoc: Decodable {
    var runId: String
    var protocolId: String?
    var protocolName: String?
    var startedAt: Double?
    var endedAt: Double?
    var status: String
    var record: RecordSnapshot

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", protocolId = "protocol_id", protocolName = "protocol_name",
             startedAt = "started_at", endedAt = "ended_at", status, record
    }
}

struct HealthInfo: Decodable {
    var ok: Bool
    var commit: String?
    var tools: Int?
    var apiVersion: Int?

    enum CodingKeys: String, CodingKey {
        case ok, commit, tools, apiVersion = "api_version"
    }
}

struct DraftLiquid: Decodable {
    var name: String
    var concentration: String?
    var prep: String?
    var quote: String
    var quoteSource: String?
    var quotePage: Int?

    enum CodingKeys: String, CodingKey {
        case name, concentration, prep, quote, quoteSource = "quote_source",
             quotePage = "quote_page"
    }
}

struct DraftCondition: Decodable {
    var label: String
    var treatments: [String]
    var replicates: Int
    var wells: [String]
    var quote: String
    var quoteSource: String?
    var quotePage: Int?

    enum CodingKeys: String, CodingKey {
        case label, treatments, replicates, wells, quote, quoteSource = "quote_source",
             quotePage = "quote_page"
    }
}

struct DraftTarget: Decodable {
    var type: String
    var value: String?
}

struct DraftPipette: Decodable {
    var liquid: String
    var volume: Double?
    var unit: String?
    var target: DraftTarget
    var mix: String?
    var repeatCount: Int?

    enum CodingKeys: String, CodingKey {
        case liquid, volume, unit, target, mix, repeatCount = "repeat"
    }
}

struct DraftStep: Decodable {
    var kind: String
    var text: String
    var quote: String
    var quoteSource: String?
    var quotePage: Int?
    var minutes: Double?
    var tempC: Double?
    var caution: String?
    var pipette: DraftPipette?
    var wells: [String]

    enum CodingKeys: String, CodingKey {
        case kind, text, quote, quoteSource = "quote_source", quotePage = "quote_page",
             minutes, tempC = "temp_c", caution, pipette, wells
    }
}

struct DraftPlan: Decodable {
    var title: String?
    var liquids: [String: DraftLiquid]
    var conditions: [String: DraftCondition]
    var steps: [DraftStep]
    var backups: [String]
}

struct DraftIssue: Decodable, Identifiable {
    var id: String { path + code }
    var path: String
    var code: String
    var reason: String
    var quote: String?
}

struct DraftMeta: Decodable {
    var source: String
    var layout: [String: String]
}

struct ProtocolDraft: Decodable {
    var plan: DraftPlan
    var review: [DraftIssue]
    var errors: [DraftIssue]
    var meta: DraftMeta
}

struct RecordSnapshot: Decodable {
    var epoch: String?
    var view = ViewState()
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
    var draft: ProtocolDraft?
    var run: RunBlock?
    var recent: [HistoryEntry]

    enum CodingKeys: String, CodingKey {
        case epoch, view, version, plan, stepIndex = "step_index", done, excluded, redo,
             notes, tips, timers, stamps, cards, draft, run, recent
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        epoch = try c.decodeIfPresent(String.self, forKey: .epoch)
        view = (try? c.decodeIfPresent(ViewState.self, forKey: .view)) ?? ViewState()
        version = try c.decode(Int.self, forKey: .version)
        plan = try c.decode(Plan.self, forKey: .plan)
        // Additive tolerance: missing/null collections default, but present
        // wrong types still throw so the banner names the break (draft is
        // auxiliary: its errors never take down the bench).
        stepIndex = try c.decodeIfPresent(Int.self, forKey: .stepIndex) ?? 0
        done = try c.decodeIfPresent([String].self, forKey: .done) ?? []
        excluded = try c.decodeIfPresent([String: String].self, forKey: .excluded) ?? [:]
        redo = try c.decodeIfPresent([String: String].self, forKey: .redo) ?? [:]
        notes = try c.decodeIfPresent([Memo].self, forKey: .notes) ?? []
        tips = try c.decodeIfPresent([SavedTip].self, forKey: .tips) ?? []
        timers = try c.decodeIfPresent([TimerEntry].self, forKey: .timers) ?? []
        stamps = try c.decodeIfPresent([String: Stamp].self, forKey: .stamps) ?? [:]
        cards = (try c.decodeIfPresent([String: ServerCard].self, forKey: .cards) ?? [:])
            .values.sorted { $0.ts > $1.ts }
        draft = (try? c.decodeIfPresent(ProtocolDraft.self, forKey: .draft)) ?? nil
        run = try c.decodeIfPresent(RunBlock.self, forKey: .run)
        recent = try c.decodeIfPresent([HistoryEntry].self, forKey: .recent) ?? []
    }
}

/// One-line reason for the out-of-sync banner: "plan.steps[2].wells missing".
func syncReason(_ error: Error) -> String {
    guard let de = error as? DecodingError else { return "unreadable snapshot" }
    let path: String
    let what: String
    switch de {
    case .keyNotFound(let k, let ctx):
        path = ((ctx.codingPath.map(\.stringValue) + [k.stringValue]).joined(separator: "."))
        what = "missing"
    case .typeMismatch(_, let ctx):
        path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
        what = "wrong type"
    case .valueNotFound(_, let ctx):
        path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
        what = "null"
    case .dataCorrupted(let ctx):
        path = ctx.codingPath.map(\.stringValue).joined(separator: ".")
        what = ctx.debugDescription
    @unknown default:
        return "unreadable snapshot"
    }
    let reason = path.isEmpty ? what : "\(path) \(what)"
    return String(reason.prefix(80))
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

@MainActor
@Observable
final class LabState {
    var epoch: String?
    var view = ViewState()
    var version = -1
    var planSteps: [PlanStep] = []
    var conditions: [String: PlanCondition] = [:]
    var steps: [String] = ["Prep", "Dispense", "Incubate", "Measure", "Clean up"]
    var stepIndex = 0
    var done: Set<String> = []
    var excluded: [String: String] = [:]
    var redo: [String: String] = [:]
    var notes: [Memo] = []
    var timers: [TimerEntry] = []
    var cards: [ServerCard] = []
    var draft: ProtocolDraft?
    var run: RunBlock?
    var history: [HistoryEntry] = []
    var lastShown = ""
    /// Per-well provenance for the well card: source and last record time.
    var wellSource: [String: String] = [:]
    var wellTime: [String: Date] = [:]
    /// Wells lit by show(), cleared 2 s after the last call.
    var flashing: Set<String> = []
    private var flashGen = 0

    /// Replace everything with a server snapshot. A new epoch (data reset,
    /// server move) resets the stale guard; within an epoch, older versions
    /// are ignored.
    func sync(_ snap: RecordSnapshot) {
        if snap.epoch != epoch {
            epoch = snap.epoch
        } else {
            guard snap.version > version else { return }
        }
        version = snap.version
        view = snap.view
        planSteps = snap.plan.steps
        conditions = snap.plan.conditions
        if !snap.plan.steps.isEmpty { steps = snap.plan.steps.map(\.text) }
        stepIndex = min(max(snap.stepIndex, 0), steps.count - 1)
        done = Set(snap.done)
        excluded = snap.excluded
        redo = snap.redo
        notes = snap.notes
        timers = snap.timers
        cards = snap.cards
        draft = snap.draft
        run = snap.run
        history = snap.recent
        wellSource = snap.stamps.mapValues(\.source)
        wellTime = snap.stamps.mapValues { Date(timeIntervalSince1970: $0.ts) }
    }

    /// Per-condition usable-done counts: done wells minus excluded ones, vs replicates.
    func conditionProgress() -> [(key: String, label: String, have: Int, need: Int)] {
        let usable = done.subtracting(excluded.keys)
        return conditions.sorted { $0.key < $1.key }.map {
            (key: $0.key, label: $0.value.label,
             have: usable.intersection($0.value.wells).count, need: $0.value.replicates)
        }
    }

    func conditionOf(_ well: String) -> String? {
        conditions.first { $0.value.wells.contains(well) }?.key
    }

    func conditionLabel(_ well: String) -> String? {
        conditions.first { $0.value.wells.contains(well) }?.value.label
    }

    var currentTip: String? {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return nil }
        return planSteps[stepIndex].tip
    }

    var currentCaution: String? {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return nil }
        return planSteps[stepIndex].caution
    }

    var currentKindLine: String? {
        guard !planSteps.isEmpty, stepIndex < planSteps.count else { return nil }
        let s = planSteps[stepIndex]
        var bits = [s.kind]
        if let m = s.minutes { bits.append(fmtNum(m) + " min") }
        if let t = s.tempC { bits.append(fmtNum(t) + " °C") }
        if let p = s.pipette, let v = p.volume {
            bits.append(fmtNum(v) + " " + (p.unit ?? "uL") + " " + p.liquid)
        }
        return bits.joined(separator: " · ")
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
        ScreenDef(id: "protocol_review", commands: ["import a protocol", "start the run", "back"]),
        ScreenDef(id: "home", commands: ["import a protocol", "open the demo protocol", "show yesterday's run"]),
        ScreenDef(id: "setup", commands: ["check the microphone", "back"]),
        ScreenDef(id: "protocol_detail", commands: ["start a new run", "show the runs", "back"]),
        ScreenDef(id: "run_view", commands: ["show yesterday's run", "read the notes", "back to the run"]),
        ScreenDef(id: "protocol_source", commands: ["show me the original for this step", "back"]),
    ]
}
