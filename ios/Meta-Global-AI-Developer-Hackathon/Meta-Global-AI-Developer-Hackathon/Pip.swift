import Foundation
import os
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
    /// Screen title, <= 4 words (server labels.step_short_title).
    var shortTitle: String?
    var minutes: Double?
    var tempC: Double?
    var tip: String?
    var caution: String?
    var pipette: PlanPipette?
    var wells: [String]
    var quote: String?
    var quotePage: Int?

    enum CodingKeys: String, CodingKey {
        case kind, text, shortTitle = "short_title", minutes, tempC = "temp_c", tip, caution, pipette, wells,
             quote, quotePage = "quote_page"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        text = try c.decode(String.self, forKey: .text)
        shortTitle = try c.decodeIfPresent(String.self, forKey: .shortTitle)
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
    /// Plate letters ("Ct", "A"), <= 3 chars (server labels.condition_abbrs).
    var abbr: String
    var treatments: [String]
    var replicates: Int
    var wells: [String]

    /// A draft's condition, for drawing the plan before it starts.
    init(_ d: DraftCondition) {
        label = d.label
        abbr = d.abbr ?? ""
        treatments = d.treatments
        replicates = d.replicates
        wells = d.wells
    }

    enum CodingKeys: String, CodingKey {
        case label, abbr, treatments, replicates, wells
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        abbr = try c.decodeIfPresent(String.self, forKey: .abbr) ?? ""
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
    /// voice / tap / typed (absent on older notes).
    var source: String?
    /// 1-based step the note was taken in (absent on older notes).
    var step: Int?
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
    /// The header chip's name, made by the server (its step's short title); drawn as is.
    var chip: String

    enum CodingKeys: String, CodingKey {
        case id, label, minutes, onDone = "on_done", started = "started_at", step, wells, chip
    }

    init(id: String, label: String, minutes: Double, onDone: String? = nil,
         started: Date = Date(), step: String = "", wells: String = "", chip: String = "") {
        self.id = id
        self.label = label
        self.minutes = minutes
        self.onDone = onDone
        self.started = started
        self.step = step
        self.wells = wells
        self.chip = chip
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
        chip = try c.decodeIfPresent(String.self, forKey: .chip) ?? ""
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
    /// Spoken; the card shows `title` and `detail` (server-made, never split here).
    var question: String
    var title: String?
    var detail: String?
    /// Small line over the title ("Step 1 · Add medium"), when the kind has one.
    var context: String?
    /// The header's one line while this card waits ("B2–B8 not marked.").
    var header: String?
    var ts: Double
    /// What a yes would do (op and its wells / step); drawn, never acted on here.
    var payload: CardPayload?
}

/// The parts of a card's payload the card screens draw. Lenient: payloads
/// differ per op, and an unknown shape never breaks the snapshot.
struct CardPayload: Decodable, Equatable {
    var op: String?
    var well: String?
    var wells: [String]?
    var step: Int?
    var target: Int?
    var reason: String?

    enum CodingKeys: String, CodingKey { case op, well, wells, step, target, reason }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        op = try? c.decodeIfPresent(String.self, forKey: .op)
        well = try? c.decodeIfPresent(String.self, forKey: .well)
        wells = try? c.decodeIfPresent([String].self, forKey: .wells)
        step = try? c.decodeIfPresent(Int.self, forKey: .step)
        target = try? c.decodeIfPresent(Int.self, forKey: .target)
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
    }

    /// Wells the card is about.
    var allWells: [String] { (wells ?? []) + (well.map { [$0] } ?? []) }
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
    /// Milo's line for this screen (registry prompt, filled by the server).
    var prompt = ""
    /// The prompt as TTS says it (server speak_text: symbols as words).
    var promptSpeak = ""
    /// Values the screen's templated buttons fill from ("Step {step}").
    var slots: [String: String] = [:]

    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        current = try c.decodeIfPresent(ViewTarget.self, forKey: .current) ?? ViewTarget()
        stack = try c.decodeIfPresent([ViewTarget].self, forKey: .stack) ?? []
        tab = try c.decodeIfPresent(String.self, forKey: .tab) ?? ""
        sheet = try c.decodeIfPresent(ViewSheet.self, forKey: .sheet) ?? ViewSheet()
        prompt = try c.decodeIfPresent(String.self, forKey: .prompt) ?? ""
        promptSpeak = try c.decodeIfPresent(String.self, forKey: .promptSpeak) ?? prompt
        slots = try c.decodeIfPresent([String: String].self, forKey: .slots) ?? [:]
    }

    private enum CodingKeys: String, CodingKey {
        case current, stack, tab, sheet, prompt, slots, promptSpeak = "prompt_speak"
    }
}

// MARK: - Library + runs

struct RunBlock: Decodable, Equatable {
    var runId: String
    var protocolId: String?
    var protocolName: String?
    var startedAt: Double
    /// Server display label for the active run.
    var label: String?

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", protocolId = "protocol_id",
             protocolName = "protocol_name", startedAt = "started_at", label
    }
}

struct ProtocolSummary: Decodable, Identifiable {
    var id: String
    var name: String
    var sourceFilename: String?
    var importedAt: Double?
    var conditions: Int
    var steps: Int
    /// Server label (server/labels.py): "Oct 29 · 12:51 AM · Drug A/B Dosing".
    var label: String?
    /// Server short title (<= 4 words), wells the plan uses, its run counts.
    var shortTitle: String?
    var wells: Int?
    var runs: Int?
    var inProgress: Int?
    /// The row's state from the server (registry `items.protocol`): active /
    /// paused / done / new, and whether it takes taps at all.
    var status: String?
    var enabled: Bool?
    /// Device key of the PDF or photos (OriginalStore); empty for older imports.
    var originalId: String?

    var title: String { shortTitle ?? name }

    enum CodingKeys: String, CodingKey {
        case id, name, sourceFilename = "source_filename", importedAt = "imported_at",
             conditions, steps, label, shortTitle = "short_title", wells, runs, inProgress = "in_progress",
             status, enabled, originalId = "original_id"
    }
}

struct ProtocolDoc: Decodable {
    var id: String
    var name: String
    var sourceFilename: String?
    var importedAt: Double?
    var plan: Plan
    /// pdf | text | photo; the text a text/photo import was checked against;
    /// the device key of the PDF or photos (OriginalStore).
    var origin: String?
    var sourceText: String?
    var originalId: String?

    enum CodingKeys: String, CodingKey {
        case id, name, sourceFilename = "source_filename", importedAt = "imported_at", plan,
             origin, sourceText = "source_text", originalId = "original_id"
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
    /// Server labels (server/labels.py): "Run 3", "Today", "Step 4 of 5" /
    /// "Ended at step 3" / "Finished" (never the run id).
    var label: String?
    var number: Int?
    var day: String?
    var state: String?

    var id: String { runId }

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", protocolId = "protocol_id", protocolName = "protocol_name",
             startedAt = "started_at", endedAt = "ended_at", status, summary, label, number, day, state
    }
}

/// Where the owner's turn is on the server (live `stage` / SSE kind "stage";
/// server api._stage): heard, thinking, searching, summarizing, done, failed.
/// The voice state follows these; `say` is a line to speak once.
struct TurnStage: Decodable, Equatable {
    var stage: String
    var text: String?
    var query: String?
    var searchId: String?
    var say: String?
    var reply: String?
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case stage, text, query, say, reply, reason, searchId = "search_id"
    }
}

/// One web source an answer cites (Spark url_citation).
struct WebSource: Decodable, Equatable {
    var title: String
    var url: String
    var domain: String
}

/// One answered web search (server/search.py); day/time are server labels.
struct WebSearch: Decodable, Identifiable, Equatable {
    var id: String
    var ts: Double
    var question: String
    var answerShort: String
    var detail: String
    var sources: [WebSource]
    var runId: String?
    var day: String
    var time: String
    /// The time as Milo says it ("12:08 AM"), for the answer card.
    var clock: String?
    /// "cited": the answer cites its sources; "pages": only the pages searched.
    var basis: String?
    /// pending (searching now), ok, or why not: no_source / timeout / canceled / error.
    var status: String?
    /// Why not, as Milo said it (failed searches).
    var line: String?
    /// "photo": a question answered from a photo (looking -> reading -> ok);
    /// nil for a plain web search.
    var kind: String?
    /// What SAM looked for ("label", "tube").
    var target: String?
    /// The thing's outline on the kept crop, 0-1 (x, y) points.
    var outline: [[Double]]?
    /// Which photo this is (Retake moves it on; a reply for an older one is ignored).
    var attempt: Int?

    var cited: Bool { basis != "pages" }
    var isPhoto: Bool { kind == "photo" }

    enum CodingKeys: String, CodingKey {
        case id, ts, question, detail, sources, day, time, clock, basis, status, line, kind, target, outline, attempt
        case answerShort = "answer_short", runId = "run_id"
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
    /// Plate letters (server rule, as for the run plan).
    var abbr: String?
    var treatments: [String]
    var replicates: Int
    var wells: [String]
    var quote: String
    var quoteSource: String?
    var quotePage: Int?

    enum CodingKeys: String, CodingKey {
        case label, abbr, treatments, replicates, wells, quote, quoteSource = "quote_source",
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

struct DraftEvidence: Decodable {
    var text: String
    var pages: [String]?
}

struct DraftMeta: Decodable {
    var source: String
    var layout: [String: String]
    /// pdf | text | photo, and the device key of the PDF or photos.
    var origin: String?
    var originalId: String?
    var evidence: DraftEvidence?

    enum CodingKeys: String, CodingKey {
        case source, layout, origin, originalId = "original_id", evidence
    }
}

/// One robot (pipetting) step of a draft as the Start screens show it.
struct DraftRow: Decodable, Identifiable {
    var n: Int
    var index: Int
    var title: String
    var detail: String
    var flagged: Bool
    var reason: String
    var quote: String
    var page: Int?
    /// A final protocol's row (⑨0): the plan's and the run's "volume · wells",
    /// and what changed with the record event it came from.
    var plan: String?
    var run: String?
    var changes: [RunChange]?
    var id: Int { n }
}

/// One change from the plan on a final protocol step (server handoff.changes).
struct RunChange: Decodable, Hashable {
    /// excluded | skipped | not_marked | redo | off_plan | timer
    var kind: String
    var wells: [String]
    var text: String
    /// The record event: excluded | skipped | redo | done | timer | none.
    var event: String
}

/// Server-made view of a draft (store snapshot draft.summary).
struct DraftSummary: Decodable {
    var rows: [DraftRow]
    var others: [String]
    var conditions: Int
    var wells: Int
    var steps: Int
    /// A final protocol: robot steps in the plan, and how many rows changed.
    var of: Int?
    var changed: Int?
}

/// Snapshot `handoff`: the finished run a hand off screen shows (⑨0-⑨d).
struct Handoff: Decodable {
    var runId: String
    var label: String
    /// ⑨0's rows: the import review's shape, rows with plan / run / changes.
    var summary: DraftSummary
    /// The protocol's original (Open PDF), as a draft's meta.
    var meta: DraftMeta
    var approved: Bool
    /// generating | simulating | fixing | passed | needs_you (nil: not approved).
    var stage: String?
    var attempt: Int
    var error: String
    var check: Check?
    var code: String
    var csv: String
    var exported: Double?
    /// The approved steps the virtual robot replays on ⑨ (empty until approved).
    var steps: [Step]?
    /// The reservoir: each liquid's well, volume and color (⑨b's line).
    var liquids: [Liquid]?

    struct Step: Decodable {
        var n: Int
        var title: String
        /// transfer | delay
        var kind: String
        var wells: [String]?
        var volumeUl: Double?
        /// A wait, in seconds (0.3 s stays 0.3 s).
        var seconds: Double?
        enum CodingKeys: String, CodingKey { case n, title, kind, wells, volumeUl = "volume_ul", seconds }

        /// "10 µL" / "18 s" / "30 min", as v36's sim rows read.
        var amount: String {
            if kind == "delay" { return Handoff.secondsText(seconds ?? 0) }
            return volumeUl.map { "\($0.formatted()) µL" } ?? ""
        }
    }

    /// One container of a liquid: reservoir well or 1.5 mL tube, filled with
    /// what the run draws + 10 % + its dead volume.
    struct Liquid: Decodable {
        var name: String
        /// reservoir | tubes
        var labware: String
        var well: String
        var volumeUl: Double
        var color: String
        enum CodingKeys: String, CodingKey { case name, labware, well, volumeUl = "volume_ul", color }

        /// "Res A1 Culture medium 4.1 mL" / "Tube A1 Vehicle 127 µL".
        var line: String {
            let v = volumeUl >= 1000 ? "\((volumeUl / 1000).formatted(.number.precision(.fractionLength(0...1)))) mL"
                : "\(Int(volumeUl)) µL"
            return "\(labware == "tubes" ? "Tube" : "Res") \(well) \(name) \(v)"
        }
    }

    /// Seconds as people say them (server handoff.secs_text): "<1 s", "18 s", "30 min".
    static func secondsText(_ s: Double) -> String {
        if s < 1 { return "<1 s" }
        if s < 120 { return "\(Int(s.rounded())) s" }
        let t = Int(s.rounded()), m = t / 60, r = t % 60
        return r == 0 ? "\(m) min" : "\(m) min \(r) s"
    }

    struct Check: Decodable {
        var ok: Bool
        var steps: [Step]
        var problems: [String]
        struct Step: Decodable { var n: Int; var title: String; var ok: Bool; var why: String }
    }

    enum CodingKeys: String, CodingKey {
        case runId = "run_id", label, summary, meta, approved, stage, attempt, error, check, code, csv, exported, steps,
             liquids
    }
}

struct ProtocolDraft: Decodable {
    var plan: DraftPlan
    var review: [DraftIssue]
    var errors: [DraftIssue]
    var meta: DraftMeta
    var summary: DraftSummary?
}

/// One per-step completion (API 5): which plan step (1-based) a well was
/// done in, by whom, when. Exclusions stay per well across steps.
struct DoneMark: Decodable, Equatable {
    var step: Int
    var well: String
    var source: String
    var ts: Double
}

struct RecordSnapshot: Decodable {
    var epoch: String?
    var view = ViewState()
    var version: Int
    var plan: Plan
    var stepIndex: Int
    var done: [DoneMark]
    var excluded: [String: String]
    var redo: [String: String]
    var notes: [Memo]
    var tips: [SavedTip]
    var timers: [TimerEntry]
    var stamps: [String: Stamp]
    var cards: [ServerCard]
    var draft: ProtocolDraft?
    var handoff: Handoff?
    var run: RunBlock?
    var recent: [HistoryEntry]
    /// The header's mode label, decided by the server (nil: not drawn).
    var headerMode: RunModeLabel?
    /// The bench as the server worked it out from the run so far.
    var bench: Bench?

    /// Snapshot `bench`: each touched well's state (done / now / excluded),
    /// wells per condition (legend), the last steps passed (Done rows).
    struct Bench: Decodable, Equatable {
        var wells: [String: String]
        var legend: [Legend]
        var rows: [Row]
        struct Legend: Decodable, Equatable { var key: String; var label: String; var n: Int }
        struct Row: Decodable, Equatable { var wells: String; var name: String; var state: String }
    }

    /// The run mode as the header shows it (`header.mode`): id full / private, its label.
    struct RunModeLabel: Decodable, Equatable {
        var id: String
        var label: String
    }

    private struct Header: Decodable { var mode: RunModeLabel? }

    enum CodingKeys: String, CodingKey {
        case epoch, view, version, plan, stepIndex = "step_index", done, excluded, redo,
             notes, tips, timers, stamps, cards, draft, handoff, run, recent, header, bench
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
        done = try c.decodeIfPresent([DoneMark].self, forKey: .done) ?? []
        excluded = try c.decodeIfPresent([String: String].self, forKey: .excluded) ?? [:]
        redo = try c.decodeIfPresent([String: String].self, forKey: .redo) ?? [:]
        notes = try c.decodeIfPresent([Memo].self, forKey: .notes) ?? []
        tips = try c.decodeIfPresent([SavedTip].self, forKey: .tips) ?? []
        timers = try c.decodeIfPresent([TimerEntry].self, forKey: .timers) ?? []
        stamps = try c.decodeIfPresent([String: Stamp].self, forKey: .stamps) ?? [:]
        cards = (try c.decodeIfPresent([String: ServerCard].self, forKey: .cards) ?? [:])
            .values.sorted { $0.ts > $1.ts }
        draft = (try? c.decodeIfPresent(ProtocolDraft.self, forKey: .draft)) ?? nil
        handoff = (try? c.decodeIfPresent(Handoff.self, forKey: .handoff)) ?? nil  // auxiliary, like draft
        run = try c.decodeIfPresent(RunBlock.self, forKey: .run)
        recent = try c.decodeIfPresent([HistoryEntry].self, forKey: .recent) ?? []
        headerMode = try c.decodeIfPresent(Header.self, forKey: .header)?.mode
        bench = try c.decodeIfPresent(Bench.self, forKey: .bench)
    }
}

/// One-line reason for the out-of-sync banner: "plan.steps[2].wells missing".
func syncReason(_ error: Error) -> String {
    // not a decode problem: say what it was (iPad: "unreadable snapshot" was a
    // GET /record that never reached the server)
    if let e = error as? PipError, case .server(let code, _) = e { return "server error \(code)" }
    if let e = error as? URLError { return "server unreachable (\(e.code.rawValue)): \(e.localizedDescription)".prefixed(80) }
    guard let de = error as? DecodingError else { return String(error.localizedDescription.prefix(80)) }
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

private extension String {
    func prefixed(_ n: Int) -> String { String(prefix(n)) }
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

/// Row-major run join, as the server's join_wells_ranges:
/// [D2, D6, D7, D8] -> "D2, D6–D8". Empty for no wells.
func wellRuns(_ wells: [String]) -> String {
    let pts = Set(wells.map { $0.uppercased() }).compactMap { w -> (String, Int)? in
        guard let col = Int(w.dropFirst()), (1...12).contains(col),
              let row = w.first.map(String.init), "ABCDEFGH".contains(row) else { return nil }
        return (row, col)
    }.sorted { ($0.0, $0.1) < ($1.0, $1.1) }
    var runs: [(String, Int, Int)] = []
    for (row, col) in pts {
        if let last = runs.last, last.0 == row, col == last.2 + 1 {
            runs[runs.count - 1].2 = col
        } else {
            runs.append((row, col, col))
        }
    }
    return runs.map { $0.1 == $0.2 ? "\($0.0)\($0.1)" : "\($0.0)\($0.1)–\($0.0)\($0.2)" }
        .joined(separator: ", ")
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
    /// Wells done in the current step (the plate's filled wells).
    var done: Set<String> = []
    /// Wells done only in other steps (dimmed on the plate).
    var doneEarlier: Set<String> = []
    /// Every (step, well) done mark, for a well's history.
    var doneMarks: [DoneMark] = []
    var excluded: [String: String] = [:]
    var redo: [String: String] = [:]
    var notes: [Memo] = []
    var timers: [TimerEntry] = []
    /// The header's mode label (server `header.mode`); nil: hidden on this screen.
    var headerMode: RecordSnapshot.RunModeLabel?
    /// The server's bench (plate well states, legend, Done rows); drawn as is.
    var bench: RecordSnapshot.Bench?
    var cards: [ServerCard] = []
    var draft: ProtocolDraft?
    var handoff: Handoff?
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
        let now = snap.done.filter { $0.step == stepIndex + 1 }.map(\.well)
        done = Set(now)
        doneEarlier = Set(snap.done.map(\.well)).subtracting(now)
        doneMarks = snap.done
        excluded = snap.excluded
        redo = snap.redo
        notes = snap.notes
        timers = snap.timers
        headerMode = snap.headerMode
        bench = snap.bench
        cards = snap.cards
        draft = snap.draft
        handoff = snap.handoff
        run = snap.run
        history = snap.recent
        wellSource = snap.stamps.mapValues(\.source)
        wellTime = snap.stamps.mapValues { Date(timeIntervalSince1970: $0.ts) }
    }


    func conditionOf(_ well: String) -> String? {
        conditions.first { $0.value.wells.contains(well) }?.key
    }

    /// Plan order of a condition (sorted keys), for its plate tone.
    func groupIndex(_ key: String?) -> Int? {
        key.flatMap { conditions.keys.sorted().firstIndex(of: $0) }
    }

    /// Current-step targets not done or excluded yet.
    var remaining: [String] {
        currentPlanned.subtracting(done).subtracting(excluded.keys).sorted()
    }

    /// Plate letters of a well's group ("Ct"), nil when it has none.
    func abbrOf(_ well: String) -> String? {
        conditions.first { $0.value.wells.contains(well) }.flatMap { $0.value.abbr.isEmpty ? nil : $0.value.abbr }
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

    /// Steps a well was done in, latest first.
    func doneSteps(_ well: String) -> [Int] {
        Array(Set(doneMarks.filter { $0.well == well }.map(\.step))).sorted(by: >)
    }

    /// "100 µL culture medium" (a pipette step's amount), else the step's title.
    func stepLine(_ step: Int) -> String {
        guard step >= 1, step <= planSteps.count else { return "" }
        let s = planSteps[step - 1]
        if let p = s.pipette, let v = p.volume {
            let unit = p.unit ?? "uL"
            return [fmtNum(v) + " " + (unit.lowercased() == "ul" ? "µL" : unit), p.liquid]
                .filter { !$0.isEmpty }.joined(separator: " ")
        }
        return s.shortTitle.flatMap { $0.isEmpty ? nil : $0 } ?? s.text
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
    /// "Say X" buttons: tap and voice run the same action.
    var buttons: [ScreenButton]?
    /// The same buttons as another screen (protocol_detail keeps Home's).
    var buttonsFrom: String?

    enum CodingKeys: String, CodingKey {
        case id, commands, buttons, buttonsFrom = "buttons_from"
    }
}

/// One registry button: `say` is shown, spoken, and listed in `commands`;
/// `action`/`args` go to /input like any tap.
struct ScreenButton: Decodable, Equatable {
    var say: String
    var action: String
    var args: [String: String]?
    /// Small line under a choice card ("PDF · Word"); plain buttons have none.
    var line: String?
    /// An iOS picker we can't drive by voice ("Browse files…"): tap only.
    var tapOnly: Bool?
    /// Shown only in this state of the screen's slots ({"status": "finished"}).
    var when: [String: String]?
    /// "disabled": drawn greyed (a tap or the words still run the action,
    /// which answers why), with `note` beside it ("Finished").
    var look: String?
    var note: String?

    var greyed: Bool { look == "disabled" }

    enum CodingKeys: String, CodingKey {
        case say, action, args, line, when, look, note, tapOnly = "tap_only"
    }

    /// A templated button ("Step {step}") filled from the screen's slots;
    /// nil while a slot it needs has no value (the server hides it too).
    func filled(_ slots: [String: String]) -> ScreenButton? {
        func fill(_ s: String) -> String? {
            var out = s
            while let r = out.range(of: #"\{(\w+)\}"#, options: .regularExpression) {
                guard let v = slots[String(out[r].dropFirst().dropLast())] else { return nil }
                out.replaceSubrange(r, with: v)
            }
            return out
        }
        guard (when ?? [:]).allSatisfy({ slots[$0.key] == $0.value }), let say = fill(say) else { return nil }
        var a: [String: String]? = nil
        if let args {
            a = [:]
            for (k, v) in args { guard let f = fill(v) else { return nil }; a?[k] = f }
        }
        return ScreenButton(say: say, action: action, args: a, line: line, tapOnly: tapOnly, when: when, look: look,
                            note: note)
    }
}

/// Screen registry shared with the server (the bundled screens.json): the
/// one source. Only `screens` is read; other keys (reads, use/not, ...) are
/// the server's. No built-in fallback: a failure comes back as a reason the
/// app shows, never as a silent substitute list.
struct ScreenRegistry: Decodable {
    var screens: [ScreenDef]
    /// Card answer buttons by card layout (single / next_step / checklist):
    /// action "answer_card", args.answer one of the registry `answers`.
    var cards: [String: [ScreenButton]]?
    static func decode(_ data: Data) throws -> ScreenRegistry {
        var reg = try JSONDecoder().decode(ScreenRegistry.self, from: data)
        guard !reg.screens.isEmpty else { throw RegistryError.empty }
        let all = reg.screens
        for i in reg.screens.indices {
            if let from = reg.screens[i].buttonsFrom {
                reg.screens[i].buttons = all.first { $0.id == from }?.buttons
            }
        }
        return reg
    }

    enum RegistryError: Error { case missing, empty }
}

/// Loaded registry, or the reason it couldn't be (shown as a banner).
func loadScreens(from url: URL? = Bundle.main.url(forResource: "screens", withExtension: "json"))
    -> (screens: [ScreenDef], cards: [String: [ScreenButton]], error: String?) {
    do {
        guard let url else { throw ScreenRegistry.RegistryError.missing }
        let reg = try ScreenRegistry.decode(Data(contentsOf: url))
        return (reg.screens, reg.cards ?? [:], nil)
    } catch {
        let reason = switch error {
        case ScreenRegistry.RegistryError.missing: "screens.json not in the app bundle"
        case ScreenRegistry.RegistryError.empty: "screens.json has no screens"
        default: syncReason(error)
        }
        Log.view.error("[view] screen registry unreadable: \(reason, privacy: .public)")
        return ([], [:], reason)
    }
}
