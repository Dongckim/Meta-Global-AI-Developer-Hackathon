import SwiftUI

func groupColor(_ name: String?) -> Color {
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

struct PlateView: View {
    let lab: LabState
    @Binding var selected: WellSelect?

    var body: some View {
        GeometryReader { g in
            let cell = min(g.size.width / 12.8, g.size.height / 8.8)
            let d = cell * 0.74
            let labelFont = Font.system(size: max(9, d * 0.3))
            VStack(spacing: 0) {
                HStack(spacing: 0) {
                    Color.clear.frame(width: cell * 0.8, height: cell * 0.8)
                    ForEach(1...12, id: \.self) { c in
                        Text("\(c)").font(labelFont).foregroundStyle(.secondary)
                            .frame(width: cell, height: cell * 0.8)
                    }
                }
                ForEach(plateRows, id: \.self) { r in
                    HStack(spacing: 0) {
                        Text(r).font(labelFont).foregroundStyle(.secondary)
                            .frame(width: cell * 0.8, height: cell)
                        ForEach(1...12, id: \.self) { c in
                            WellDot(well: "\(r)\(c)", lab: lab, d: d) {
                                selected = WellSelect(id: "\(r)\(c)")
                            }
                            .frame(width: cell, height: cell)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

struct WellDot: View {
    @Environment(\.colorScheme) private var scheme
    let well: String
    let lab: LabState
    let d: CGFloat
    let onTap: () -> Void

    private var excluded: Bool { lab.excluded[well] != nil }
    private var done: Bool { lab.done.contains(well) }
    private var redo: Bool { lab.redo[well] != nil }
    private var noted: Bool { lab.hasNote(well) }
    private var group: String? { lab.groupOf(well) }
    private var current: Bool { lab.currentPlanned.contains(well) }
    private var lit: Bool { lab.flashing.contains(well) }

    private var fill: Color {
        if excluded { return scheme == .dark ? Color(white: 0.6) : Color(white: 0.35) }
        if done { return groupColor(group) }
        if current, group != nil { return groupColor(group).opacity(0.18) }
        return .clear
    }

    private var edge: (Color, CGFloat) {
        if lit { return (.yellow, 3) }
        if current { return (groupColor(group), 3) }
        if group != nil { return (groupColor(group), 1.5) }
        return (.secondary.opacity(0.5), 1.5)
    }

    private var markFg: Color { (excluded || done) ? .white : .primary }

    var body: some View {
        Button(action: onTap) {
            Circle()
                .fill(fill)
                .frame(width: d, height: d)
                .overlay(Circle().stroke(edge.0, lineWidth: edge.1).frame(width: d, height: d))
                .overlay {
                    if excluded {
                        Image(systemName: "xmark").font(.system(size: d * 0.4, weight: .bold)).foregroundStyle(markFg)
                    } else if redo {
                        Text("↻").font(.system(size: d * 0.44, weight: .bold)).foregroundStyle(markFg)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    if noted {
                        Circle().fill(.red).frame(width: d * 0.22, height: d * 0.22)
                            .offset(x: d * 0.04, y: -d * 0.04)
                    }
                }
        }
        .buttonStyle(.plain)
        .animation(.easeInOut(duration: 0.3), value: "\(excluded)\(done)\(redo)\(noted)\(lit)\(current)")
    }
}

// MARK: - Group bars

struct GroupBars: View {
    let lab: LabState

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(lab.groupProgress(), id: \.name) { g in
                let ok = g.have >= g.need
                HStack(spacing: 6) {
                    Text(g.name).font(.caption).frame(width: 56, alignment: .leading)
                    Text(String(repeating: "■", count: g.have) + String(repeating: "□", count: max(g.need - g.have, 0)))
                        .font(.caption.monospaced())
                        .foregroundStyle(ok ? .green : .orange)
                    Text("\(g.have)/\(g.need)").font(.caption.bold())
                        .foregroundStyle(ok ? .green : .orange)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 4)
    }
}

// MARK: - Well card

struct WellSheet: View {
    @Environment(\.dismiss) private var dismiss
    let well: String
    let lab: LabState
    let onLog: (String) -> Void
    @State private var editText = ""
    @State private var saving = false

    private var memos: [Memo] { lab.memos(for: well) }

    private var status: String {
        var s: [String] = []
        if lab.excluded[well] != nil { s.append("Excluded") }
        if lab.redo[well] != nil { s.append("Redo") }
        if lab.done.contains(well) { s.append("Done") }
        if s.isEmpty { s.append(lab.groupOf(well) == nil ? "Empty" : "Planned") }
        return s.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(well).font(.title.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            LabeledContent("Group", value: lab.groupOf(well) ?? "—")
            LabeledContent("Status", value: status)
            VStack(alignment: .leading, spacing: 2) {
                Text("Notes").font(.callout).foregroundStyle(.secondary)
                if memos.isEmpty {
                    Text("—").font(.callout)
                } else {
                    ForEach(memos) { m in
                        Text("• \(m.text) \(m.history.isEmpty ? "" : "(edited \(m.history.count)×)")").font(.callout)
                    }
                }
            }
            if let last = memos.last {
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
        Task {
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

struct StepCard: View {
    let lab: LabState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.headline)
            if let tip = lab.currentTip {
                Text("💡 \(tip)").font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private var title: String {
        let name = lab.steps[lab.stepIndex]
        var t = "Step \(lab.stepIndex + 1) of \(lab.steps.count) · \(name)"
        if let r = lab.currentRange, !name.contains(r) { t += " · \(r)" }
        return t
    }
}

struct TimerList: View {
    let lab: LabState
    @State private var expanded = false

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1.0)) { ctx in
            timerCard(now: ctx.date)
        }
    }

    private func timerCard(now: Date) -> some View {
        let vis = lab.visibleTimers(now: now)
        return VStack(alignment: .leading, spacing: 4) {
            Button {
                expanded.toggle()
            } label: {
                HStack {
                    Text("Timers (\(vis.count))").font(.headline)
                    Spacer()
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.plain)
            if expanded {
                if vis.isEmpty {
                    Text("No timers").font(.callout).foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        ForEach(vis, id: \.id) { t in
                            row(t, now: now)
                        }
                    }
                    .frame(maxHeight: 220)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private func row(_ t: TimerEntry, now: Date) -> some View {
        let rem = t.minutes * 60 - now.timeIntervalSince(t.started)
        let over = rem <= 0
        let blink = over && Int(now.timeIntervalSince1970 * 2) % 2 == 0
        return HStack {
            Text(t.label).font(.callout)
            Spacer()
            Text(over ? "done" : fmtClock(rem))
                .font(.callout.monospacedDigit().bold())
                .foregroundStyle(over ? .red : .primary)
        }
        .opacity(blink ? 0.25 : 1)
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
    let lab: LabState
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
