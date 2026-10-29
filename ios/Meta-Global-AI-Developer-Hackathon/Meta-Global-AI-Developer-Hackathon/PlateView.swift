import SwiftUI

func conditionColor(_ name: String?) -> Color {
    switch name {
    case "ctrl": .gray
    case "drug_A": .blue
    case "drug_B": .orange
    case .none: .secondary
    default: [.green, .purple, .pink, .teal, .mint][abs(name!.hashValue) % 5]
    }
}

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
    @State private var start = Date()
    @State private var introDone = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    private static let introLength = Theme.Motion.wellBase + 18 * Theme.Motion.wellStep + Theme.Motion.wellIn

    var body: some View {
        GeometryReader { g in
            let cell = min(g.size.width / 12.8, g.size.height / 8.8)
            let d = cell * 48 / 58
            let labelFont = Font.system(size: max(9, d / 3), weight: .medium)
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
                                        intro: intro(t, row: ri, col: c - 1)) {
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
    /// wellIn progress from the plate's clock (1 = settled).
    var intro: Double = 1
    let onTap: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var excluded: Bool { lab.excluded[well] != nil }
    private var done: Bool { lab.done.contains(well) }
    private var doneEarlier: Bool { lab.doneEarlier.contains(well) }
    private var redo: Bool { lab.redo[well] != nil }
    private var noted: Bool { lab.hasNote(well) }
    private var current: Bool { lab.currentPlanned.contains(well) }
    private var lit: Bool { lab.flashing.contains(well) }
    private var tone: Color { lab.groupIndex(lab.conditionOf(well)).map(Theme.groupTone) ?? Theme.text1 }

    /// Step record states: this step's done wells filled in their group
    /// tone, its remaining targets white with a blue ring, earlier steps'
    /// done wells dimmed, excluded wells empty with a cross.
    private var filled: Bool { !excluded && (done || (!current && doneEarlier)) }
    private var now: Bool { current && !done && !excluded }

    private var fill: Color {
        if filled { return done ? tone : tone.opacity(0.45) }
        return now ? Theme.card : Theme.wellEmpty
    }

    private var edge: (Color, CGFloat) {
        if lit { return (Theme.text1, d * 3 / 48) }
        if now { return (Theme.polishBlue, d * 3 / 48) }
        return (.clear, 0)
    }

    var body: some View {
        let M = Theme.Motion.self
        Button(action: onTap) {
            Circle()
                .fill(fill.shadow(.inner(color: .black.opacity(filled || now ? 0 : 0.07), radius: 1, y: 1)))
                .frame(width: d, height: d)
                .overlay(Circle().strokeBorder(edge.0, lineWidth: edge.1))
                .overlay {
                    if excluded {
                        Image(systemName: "xmark").font(.system(size: d * 0.36, weight: .medium))
                            .foregroundStyle(Theme.text2)
                    } else if redo {
                        Text("↻").font(.system(size: d * 0.4, weight: .semibold))
                            .foregroundStyle(filled ? .white : Theme.text1)
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
                .keyframeAnimator(initialValue: 1.0, trigger: done) { view, k in
                    view.scaleEffect(reduce || !done ? 1 : k)
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
        .animation(.easeInOut(duration: 0.3), value: "\(excluded)\(done)\(doneEarlier)\(redo)\(noted)\(lit)\(current)")
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
            ForEach(lab.conditionProgress(), id: \.key) { g in
                HStack(spacing: 12 * u) {
                    Circle().fill(lab.groupIndex(g.key).map(Theme.groupTone) ?? Theme.text1)
                        .frame(width: 12 * u, height: 12 * u)
                    Text(g.label).font(.system(size: 28 * u)).foregroundStyle(Theme.text1)
                    (Text("n ").foregroundStyle(Theme.text2)
                     + Text("\(g.have)").fontWeight(.semibold).foregroundStyle(Theme.text1)
                     + Text(g.have < g.need ? "/\(g.need)" : "").foregroundStyle(Theme.text2))
                        .font(.system(size: 34 * u)).tracking(-0.68 * u)
                }
                .lineLimit(1).minimumScaleFactor(0.6)
                .frame(maxWidth: .infinity)
            }
        }
    }
}

// MARK: - Well card

struct WellSheet: View {
    @Environment(\.dismiss) private var dismiss
    let well: String
    @Bindable var lab: LabState
    var readOnly = false
    let onLog: (String) -> Void
    @State private var editText = ""
    @State private var saving = false

    private var memos: [Memo] { lab.memos(for: well) }

    private var status: String {
        var s: [String] = []
        if lab.excluded[well] != nil { s.append("Excluded") }
        if lab.redo[well] != nil { s.append("Redo") }
        if lab.done.contains(well) {
            s.append("Done this step")
        } else if lab.doneEarlier.contains(well) {
            s.append("Done in an earlier step")
        }
        if s.isEmpty { s.append(lab.conditionOf(well) == nil ? "Empty" : "Planned") }
        return s.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(well).font(.title.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            LabeledContent("Condition", value: lab.conditionLabel(well) ?? "—")
            LabeledContent("Status", value: status)
            VStack(alignment: .leading, spacing: 2) {
                Text("Notes").font(.callout).foregroundStyle(.secondary)
                if memos.isEmpty {
                    Text("—").font(.callout)
                } else {
                    ForEach(memos) { m in
                        Text("• \(m.text) \(m.history.isEmpty ? "" : "(edited \(m.history.count)×)")").font(.callout)
                        if let raw = m.raw, raw != m.text {
                            DisclosureGroup("View original") {
                                Text(raw).font(.caption).foregroundStyle(.secondary)
                            }.font(.caption)
                        }
                    }
                }
            }
            if !readOnly, let last = memos.last {
                HStack {
                    TextField("Edit last note", text: $editText)
                        .textFieldStyle(.roundedBorder)
                        .onAppear { if editText.isEmpty { editText = last.text } }
                    Button(saving ? "Saving…" : "Save") { saveEdit(id: last.id) }
                        .buttonStyle(.borderedProminent)
                        .disabled(saving || editText.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            LabeledContent("Exclude reason", value: reasonText)
            LabeledContent("Recorded", value: timeText)
            LabeledContent("Source", value: lab.wellSource[well] ?? "—")
            Spacer()
        }
        .padding()
    }

    private func saveEdit(id: String) {
        saving = true
        Task { @MainActor in
            do {
                let r = try await PipClient.editNote(id: id, text: editText)
                onLog(r.message)
                dismiss()
            } catch {
                onLog("Error: \(error.localizedDescription)")
            }
            saving = false
        }
    }

    private var reasonText: String {
        guard let r = lab.excluded[well] else { return "—" }
        return r.isEmpty ? "(no reason)" : r
    }

    private var timeText: String {
        lab.wellTime[well]?.formatted(date: .omitted, time: .standard) ?? "—"
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
                .font(.system(size: 28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text2)
            Text(title)
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1)
                .lineLimit(2).truncationMode(.tail)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8 * u)
            if let detail {
                Text(detail).font(.system(size: 28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).lineLimit(1).minimumScaleFactor(0.6)
                    .padding(.top, 14 * u)
            }
            if let caution = lab.currentCaution {
                Button { onSource?() } label: {
                    Label(caution, systemImage: "exclamationmark.triangle")
                        .font(.system(size: 22 * u)).foregroundStyle(Theme.text3)
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
        if let r = lab.currentRange { bits.append(r) }
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
                ForEach(vis.prefix(maxRows)) { t in row(t, now: ctx.date) }
                if vis.count > maxRows {
                    Text("+\(vis.count - maxRows)").font(.system(size: 22 * u, weight: .medium))
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
                .font(.system(size: 26 * u, weight: .semibold)).foregroundStyle(Theme.text1)
            if !t.wells.isEmpty {
                Text(t.step).font(.system(size: 24 * u, weight: .light)).foregroundStyle(Theme.text2)
            }
            Spacer(minLength: 0)
            Text(over ? "done" : "\(Int((rem / 60).rounded(.up))) min")
                .font(.system(size: 26 * u, weight: .semibold).monospacedDigit())
                .foregroundStyle(Theme.text1)
        }
        .lineLimit(1).minimumScaleFactor(0.6)
        .padding(.horizontal, 12 * u)
        .frame(height: 43 * u)
        .background(Capsule().fill(Theme.chip).shadow(color: .black.opacity(0.04), radius: 1, y: 1))
        .opacity(blink ? 0.25 : 1)
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
            if let r = wellRangeLabel(step.wells) {
                Text(r).font(.system(size: 22 * u, weight: .semibold)).foregroundStyle(Theme.text5)
            }
            Text(step.text).font(.system(size: 21 * u, weight: .light)).foregroundStyle(Theme.text2)
            Spacer(minLength: 0)
            Text("After step \(n)").font(.system(size: 20 * u)).foregroundStyle(Theme.text2)
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
                Text("\(left.count) left").font(.system(size: 40 * u, weight: .semibold))
                    .tracking(-0.88 * u).foregroundStyle(Theme.text1)
                    .modifier(Bump(value: left.count))
            }
            if !left.isEmpty {
                Text(wellRuns(left)).font(.system(size: 28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).lineLimit(2).minimumScaleFactor(0.6)
            }
        }
        .padding(.horizontal, 36 * u).padding(.vertical, 28 * u)
        .frame(maxWidth: .infinity, minHeight: 145 * u, alignment: .leading)
        .benchCard(radius: 32 * u)
    }
}

// MARK: - Memo list

private func isWellID(_ s: String) -> Bool {
    guard s.count >= 2 else { return false }
    let u = s.uppercased()
    guard let c = Int(u.dropFirst()), (1...12).contains(c) else { return false }
    return "ABCDEFGH".contains(String(u.prefix(1)))
}

struct MemoList: View {
    @Bindable var lab: LabState
    let onSelect: (String) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                expanded.toggle()
            } label: {
                HStack {
                    Text("Notes (\(lab.notes.count))").font(.headline)
                    Spacer()
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.plain)
            if expanded {
                if lab.notes.isEmpty {
                    Text("No notes").font(.callout).foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(lab.notes) { m in
                                Button {
                                    if isWellID(m.target) { onSelect(m.target) }
                                } label: {
                                    HStack(alignment: .top, spacing: 6) {
                                        Text(m.target).font(.callout.bold())
                                            .frame(width: 32, alignment: .leading)
                                        Text(m.text).font(.callout).lineLimit(2)
                                        Spacer()
                                        if !m.history.isEmpty {
                                            Text("edited").font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    .frame(maxHeight: 160)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}
