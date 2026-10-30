import SwiftUI

struct WellSelect: Identifiable {
    let id: String
}

// MARK: - Plate

/// 96-well plate, ver 2 (Figma 37:13521): pitch 58, well 48, axis labels
/// 16. Sized from its frame; colors only from `Theme`. Motion runs on one
/// clock for all 96 wells (the wellIn wave) and one Canvas for the glow on
/// the step's remaining wells; Theme.Motion holds the values.
struct PlateView: View {
    @Bindable var lab: LabState
    @Binding var selected: WellSelect?
    /// The well whose card is open: a blue focus ring.
    var focus: String? = nil
    /// Before a run (⑤f Plan check): every condition's wells as a ring in
    /// its group tone with its letters, as the design draws the plan.
    var showPlanned = false
    @State private var start = Date()
    @State private var introDone = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    private static let introLength = Theme.Motion.wellBase + 18 * Theme.Motion.wellStep + Theme.Motion.wellIn

    var body: some View {
        GeometryReader { g in
            let cell = min(g.size.width / 12.8, g.size.height / 8.8)
            let d = cell * 48 / 58
            let labelFont = Theme.ui(max(9, d / 3), weight: .medium)
            TimelineView(.animation(paused: introDone)) { ctx in
                let t = introDone ? .infinity : ctx.date.timeIntervalSince(start)
                VStack(spacing: 0) {
                    HStack(spacing: 0) {
                        Color.clear.frame(width: cell * 0.8, height: cell * 0.8)
                        ForEach(1...12, id: \.self) { c in
                            Text("\(c)").font(labelFont).foregroundStyle(Theme.text4)
                                .frame(width: cell, height: cell * 0.8)
                        }
                    }
                    ForEach(Array(plateRows.enumerated()), id: \.offset) { ri, r in
                        HStack(spacing: 0) {
                            Text(r).font(labelFont).foregroundStyle(Theme.text4)
                                .frame(width: cell * 0.8, height: cell)
                            ForEach(1...12, id: \.self) { c in
                                WellDot(well: "\(r)\(c)", lab: lab, d: d, focused: focus == "\(r)\(c)",
                                        showPlanned: showPlanned, intro: intro(t, row: ri, col: c - 1)) {
                                    selected = WellSelect(id: "\(r)\(c)")
                                }
                                .frame(width: cell, height: cell)
                            }
                        }
                    }
                }
            }
            .overlay(alignment: .topLeading) { glow(cell: cell, d: d) }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .task {
            try? await Task.sleep(for: .seconds(Self.introLength))
            introDone = true
        }
    }

    /// wellIn progress of one well (0...1, spring overshoot): delay grows
    /// along the diagonal.
    private func intro(_ t: Double, row: Int, col: Int) -> Double {
        let M = Theme.Motion.self
        let x = (t - M.wellBase - Double(row + col) * M.wellStep) / M.wellIn
        return x >= 1 ? 1 : M.springCurve(max(0, x))
    }

    /// nowGlow: one Canvas draws the breathing outer ring of every remaining
    /// target (the step's wells not done or excluded).
    private func glow(cell: CGFloat, d: CGFloat) -> some View {
        let M = Theme.Motion.self
        let now = lab.remaining
        return TimelineView(.animation(minimumInterval: 1.0 / 30, paused: now.isEmpty || reduce)) { ctx in
            let t = ctx.date.timeIntervalSince(start) - M.nowGlowStart
            let k = t < 0 || reduce ? 0 : Keyframes.pingPong(t, period: M.nowGlowPeriod)
            let w = (M.nowGlowRing.0 + (M.nowGlowRing.1 - M.nowGlowRing.0) * k) * d / 48
            let a = M.nowGlowAlpha.0 + (M.nowGlowAlpha.1 - M.nowGlowAlpha.0) * k
            Canvas { gc, _ in
                for well in now {
                    guard let ri = plateRows.firstIndex(of: String(well.prefix(1))),
                          let c = Int(well.dropFirst()) else { continue }
                    let center = CGPoint(x: cell * 0.8 + (CGFloat(c) - 0.5) * cell,
                                         y: cell * 0.8 + (CGFloat(ri) + 0.5) * cell)
                    let r = d / 2 + w / 2
                    let ring = Path(ellipseIn: CGRect(x: center.x - r, y: center.y - r, width: 2 * r, height: 2 * r))
                    gc.stroke(ring, with: .color(Theme.polishBlue.opacity(a)), lineWidth: w)
                }
            }
            .allowsHitTesting(false)
        }
    }
}

struct WellDot: View {
    let well: String
    @Bindable var lab: LabState
    let d: CGFloat
    var focused = false
    var showPlanned = false
    /// wellIn progress from the plate's clock (1 = settled).
    var intro: Double = 1
    let onTap: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// The server's state for this well (snapshot bench.wells): done,
    /// now, excluded, or nothing. Nothing is worked out per step here.
    private var state: String? { lab.bench?.wells[well] }
    private var excluded: Bool { state == "excluded" }
    private var redo: Bool { lab.redo[well] != nil }
    private var noted: Bool { lab.hasNote(well) }
    private var lit: Bool { lab.flashing.contains(well) }
    private var tone: Color { lab.groupIndex(lab.conditionOf(well)).map(Theme.groupTone) ?? Theme.text1 }

    /// Done wells (this step or earlier) filled in their group tone, this
    /// step's remaining targets white with a blue ring, excluded wells
    /// empty with a cross (design ① Main).
    private var filled: Bool { state == "done" }
    private var now: Bool { state == "now" }
    /// Passed over unmarked (Skip them): an empty well with a faint dashed ring.
    private var skipped: Bool { state == "skipped" }
    /// A condition's well not yet done or due (plan check): a group-tone ring.
    private var planned: Bool { showPlanned && !filled && !now && !excluded && lab.conditionOf(well) != nil }

    private var fill: Color {
        if filled { return tone }
        return now || planned ? Theme.card : Theme.wellEmpty
    }

    private var edge: (Color, CGFloat) {
        if lit { return (Theme.text1, d * 3 / 48) }
        if now { return (Theme.polishBlue, d * 3 / 48) }
        if planned { return (tone, d * 2 / 48) }
        return (.clear, 0)
    }

    var body: some View {
        let M = Theme.Motion.self
        Button(action: onTap) {
            Circle()
                .fill(fill.shadow(.inner(color: .black.opacity(filled || now || planned ? 0 : 0.07), radius: 1, y: 1)))
                .frame(width: d, height: d)
                .overlay(Circle().strokeBorder(edge.0, lineWidth: edge.1))
                .overlay {
                    if skipped {
                        Circle().strokeBorder(Theme.text4, style: StrokeStyle(lineWidth: d * 2 / 48,
                                                                             dash: [d * 4 / 48, d * 4 / 48]))
                    }
                }
                .overlay {
                    if excluded {
                        Image(systemName: "xmark").font(Theme.ui(d * 0.36, weight: .medium))
                            .foregroundStyle(Theme.text2)
                    } else if redo {
                        Text("↻").font(Theme.ui(d * 0.4, weight: .semibold))
                            .foregroundStyle(filled ? .white : Theme.text1)
                    } else if filled || now || planned, let abbr = lab.abbrOf(well) {
                        // group letters: white on a filled well, blue on a remaining
                        // target, the group tone on a planned one
                        Text(abbr).font(Theme.ui(d * 17 / 48, weight: .semibold))
                            .foregroundStyle(now ? Theme.polishBlue : planned ? tone : .white)
                            .lineLimit(1).minimumScaleFactor(0.5)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if noted {
                        Circle().fill(Theme.text1)
                            .overlay(Circle().strokeBorder(.white, lineWidth: d * 2.5 / 48))
                            .frame(width: d * 11 / 48, height: d * 11 / 48)
                    }
                }
                .overlay {
                    if focused {
                        Circle().strokeBorder(Theme.polishBlue, lineWidth: d * 2 / 48)
                            .frame(width: d * 66 / 48, height: d * 66 / 48)
                    }
                }
                // justIn: a well just done pops (.6 -> 1.12 -> 1)
                .keyframeAnimator(initialValue: 1.0, trigger: filled) { view, k in
                    view.scaleEffect(reduce || !filled ? 1 : k)
                } keyframes: { _ in
                    KeyframeTrack {
                        CubicKeyframe(M.wellInScale, duration: 0.001)
                        CubicKeyframe(1.12, duration: M.justIn * 0.6)
                        CubicKeyframe(1, duration: M.justIn * 0.4)
                    }
                }
                .scaleEffect(reduce ? 1 : M.wellInScale + (1 - M.wellInScale) * intro)
                .opacity(min(1, max(0, intro)))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(well)
        .animation(.easeInOut(duration: 0.3), value: "\(state ?? "")\(redo)\(noted)\(lit)")
    }
}

// MARK: - Group legend

/// One line per plan group: dot in its plate tone, label, usable-done
/// count ("n 7"), and "/need" while short.
struct ConditionBars: View {
    @Bindable var lab: LabState
    /// Design px -> points.
    var u: CGFloat = 0.6

    var body: some View {
        HStack(spacing: 0) {
            // design ① legend: each condition and its wells on the plate ("n 7"), from the server
            ForEach(lab.bench?.legend ?? [], id: \.key) { g in
                HStack(spacing: 12 * u) {
                    Circle().fill(lab.groupIndex(g.key).map(Theme.groupTone) ?? Theme.text1)
                        .frame(width: 12 * u, height: 12 * u)
                    Text(g.label).font(Theme.ui(28 * u)).foregroundStyle(Theme.text1)
                    (Text("n ").foregroundStyle(Theme.text2)
                     + Text("\(g.n)").font(Theme.ui(34 * u, weight: .semibold)).foregroundStyle(Theme.text1))
                        .font(Theme.ui(34 * u)).tracking(-0.68 * u)
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .frame(maxWidth: .infinity)
            }
        }
    }
}

// MARK: - Side panel

/// "Step 4 of 5", the step name, its amount and wells, and one bar per
/// step (done / now / later).
struct StepCard: View {
    @Bindable var lab: LabState
    /// Design px -> points.
    var u: CGFloat = 0.6
    var onSource: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Step \(lab.stepIndex + 1) of \(lab.steps.count)")
                .font(Theme.ui(28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text2)
            Text(title)
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                .foregroundStyle(Theme.text1)
                .lineLimit(2).truncationMode(.tail)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8 * u)
            if let detail {
                Text(detail).font(Theme.ui(28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.top, 14 * u)
            }
            if let caution = lab.currentCaution {
                Button { onSource?() } label: {
                    Label(caution, systemImage: "exclamationmark.triangle")
                        .font(Theme.ui(22 * u)).foregroundStyle(Theme.text3)
                        .lineLimit(2).multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain).disabled(onSource == nil)
                .padding(.top, 10 * u)
            }
            StepBars(count: lab.steps.count, done: lab.stepIndex, current: lab.stepIndex, u: u)
                .padding(.top, 22 * u)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .enter(.rise, u: u)
        .id(lab.stepIndex)  // a new step rises in and regrows its bars
    }

    /// The step's short title ("Add Drug B"), else its sentence.
    private var title: String {
        let short = lab.stepIndex < lab.planSteps.count ? lab.planSteps[lab.stepIndex].shortTitle : nil
        return short.flatMap { $0.isEmpty ? nil : $0 } ?? lab.steps[lab.stepIndex]
    }

    /// "10 µL · D2–D8": amount, time, temperature, then the step's wells.
    private var detail: String? {
        var bits: [String] = []
        if lab.stepIndex < lab.planSteps.count {
            let s = lab.planSteps[lab.stepIndex]
            if let p = s.pipette, let v = p.volume {
                let unit = p.unit ?? "uL"
                bits.append(fmtNum(v) + " " + (unit.lowercased() == "ul" ? "µL" : unit))
            }
            if let m = s.minutes { bits.append(fmtNum(m) + " min") }
            if let t = s.tempC { bits.append(fmtNum(t) + " °C") }
        }
        if lab.stepIndex < lab.planSteps.count, !lab.planSteps[lab.stepIndex].wells.isEmpty {
            bits.append(wellRuns(lab.planSteps[lab.stepIndex].wells))  // "B2–B8, C2–C8", as "N left"
        }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }
}

/// Running timers as rows (ring, wells, step, minutes left), soonest
/// first, then the plan's next timed step ("After step 4").
struct TimerList: View {
    @Bindable var lab: LabState
    /// Design px -> points.
    var u: CGFloat = 0.6
    var maxRows = 2

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1.0)) { ctx in
            let vis = lab.visibleTimers(now: ctx.date)
                .sorted { left($0, ctx.date) < left($1, ctx.date) }
            VStack(alignment: .leading, spacing: 5 * u) {
                // the last steps passed stay as Done rows (server bench.rows), then timers and what's next
                ForEach(Array((lab.bench?.rows ?? []).enumerated()), id: \.offset) { _, r in doneRow(r) }
                ForEach(vis.prefix(maxRows)) { t in row(t, now: ctx.date) }
                if vis.count > maxRows {
                    Text("+\(vis.count - maxRows)").font(Theme.ui(22 * u, weight: .medium))
                        .foregroundStyle(Theme.polishBlue).padding(.leading, 14 * u)
                }
                if let (n, step) = upcoming { upcomingRow(n, step) }
            }
        }
    }

    private func left(_ t: TimerEntry, _ now: Date) -> Double {
        t.minutes * 60 - now.timeIntervalSince(t.started)
    }

    private func row(_ t: TimerEntry, now: Date) -> some View {
        let rem = left(t, now)
        let over = rem <= 0
        let blink = over && Int(now.timeIntervalSince1970 * 2) % 2 == 0
        return HStack(spacing: 14 * u) {
            TimerRing(progress: t.minutes > 0 ? min(1, max(0, 1 - rem / (t.minutes * 60))) : 1,
                      size: 30 * u)
            Text(t.wells.isEmpty ? t.label : t.wells)
                .font(Theme.ui(26 * u, weight: .semibold)).foregroundStyle(Theme.text1)
            if !t.wells.isEmpty {
                Text(t.step).font(Theme.ui(24 * u, weight: .light)).foregroundStyle(Theme.text2)
            }
            Spacer(minLength: 0)
            Text(over ? "done" : "\(Int((rem / 60).rounded(.up))) min")
                .font(Theme.ui(26 * u, weight: .semibold).monospacedDigit())
                .foregroundStyle(Theme.text1)
        }
        .lineLimit(1).minimumScaleFactor(0.6)
        .padding(.horizontal, 12 * u)
        .frame(height: 43 * u)
        .background(Capsule().fill(Theme.chip).shadow(color: .black.opacity(0.04), radius: 1, y: 1))
        .opacity(blink ? 0.25 : 1)
    }

    /// Design ① "B2–B8 PBS  Done": a step passed, checked.
    private func doneRow(_ r: RecordSnapshot.Bench.Row) -> some View {
        HStack(spacing: 14 * u) {
            ZStack {
                Circle().fill(Theme.polishBlue)
                Image(systemName: "checkmark").font(Theme.ui(14 * u, weight: .bold)).foregroundStyle(.white)
            }
            .frame(width: 30 * u, height: 30 * u)
            Text(r.wells).font(Theme.ui(26 * u, weight: .semibold)).foregroundStyle(Theme.text1)
            Text(r.name).font(Theme.ui(24 * u, weight: .light)).foregroundStyle(Theme.text2)
            Spacer(minLength: 0)
            Text("Done").font(Theme.ui(26 * u, weight: .semibold)).foregroundStyle(Theme.polishBlue)
        }
        .lineLimit(1).minimumScaleFactor(0.6)
        .padding(.horizontal, 12 * u)
        .frame(height: 43 * u)
        .background(Capsule().fill(Theme.chip).shadow(color: .black.opacity(0.04), radius: 1, y: 1))
    }

    /// Next plan step with a duration, after the current one.
    private var upcoming: (Int, PlanStep)? {
        lab.planSteps.indices.dropFirst(lab.stepIndex + 1)
            .first { lab.planSteps[$0].minutes != nil }
            .map { ($0, lab.planSteps[$0]) }
    }

    private func upcomingRow(_ n: Int, _ step: PlanStep) -> some View {
        HStack(spacing: 14 * u) {
            Circle().strokeBorder(Theme.text4, style: StrokeStyle(lineWidth: 2 * u, dash: [3 * u, 3 * u]))
                .frame(width: 24 * u, height: 24 * u)
            if !step.wells.isEmpty {
                Text(wellRuns(step.wells)).font(Theme.ui(22 * u, weight: .semibold)).foregroundStyle(Theme.text5)
            }
            // "Incubate · 30 min": the step's short title and its time, never the sentence
            Text([step.shortTitle.flatMap { $0.isEmpty ? nil : $0 }, step.minutes.map { fmtNum($0) + " min" }]
                    .compactMap { $0 }.joined(separator: " · "))
                .font(Theme.ui(21 * u, weight: .light)).foregroundStyle(Theme.text2)
            Spacer(minLength: 0)
            Text("After step \(n)").font(Theme.ui(20 * u)).foregroundStyle(Theme.text2)
        }
        .lineLimit(1).minimumScaleFactor(0.6)
        .padding(.horizontal, 15 * u)
    }
}

/// "4 left" and which wells: the current step's targets not yet done.
struct RemainingCard: View {
    @Bindable var lab: LabState
    /// Design px -> points.
    var u: CGFloat = 0.6

    var body: some View {
        let left = lab.remaining
        VStack(alignment: .leading, spacing: 6 * u) {
            HStack(spacing: 14 * u) {
                Circle().fill(Theme.polishBlue.opacity(0.5)).frame(width: 14 * u, height: 14 * u)
                Text("\(left.count) left").font(Theme.ui(40 * u, weight: .semibold))
                    .tracking(-0.88 * u).foregroundStyle(Theme.text1)
                    .modifier(Bump(value: left.count))
            }
            if !left.isEmpty {
                Text(wellRuns(left)).font(Theme.ui(28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
            }
        }
        .padding(.horizontal, 36 * u).padding(.vertical, 28 * u)
        .frame(maxWidth: .infinity, minHeight: 145 * u, alignment: .leading)
        .benchCard(radius: 32 * u)
    }
}
