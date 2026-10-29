import SwiftUI

// Well card, ver 2 (Figma ④ 37:13833): the bench's side column shows one
// well (the plate stays, with the well ringed). Data: snapshot notes (text,
// source, step), done marks per (step, well), plan steps (amounts), conditions,
// exclusions, redo. Button: registry well_card `Close` (= back).

struct WellCardSide: View {
    let well: String
    @Bindable var lab: LabState
    /// Registry well_card buttons.
    var buttons: [ScreenButton]
    /// Design px -> points.
    var u: CGFloat
    var perform: (ScreenButton) -> Void

    private var memos: [Memo] { lab.memos(for: well) }

    /// "Control · Done this step": the well's group and record state.
    private var line: String {
        var state = "Empty"
        if lab.excluded[well] != nil { state = "Excluded" }
        else if lab.redo[well] != nil { state = "Redo" }
        else if lab.done.contains(well) { state = "Done this step" }
        else if lab.doneEarlier.contains(well) { state = "Done earlier" }
        else if lab.currentPlanned.contains(well) { state = "Next up" }
        else if lab.conditionOf(well) != nil { state = "Planned" }
        return [lab.conditionLabel(well), state].compactMap { $0 }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Well").font(.system(size: 28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text2)
                .enter(.rise, 0, u: u)
            Text(well).font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1).padding(.top, 8 * u)
                .enter(.text, 1, u: u)
            Text(line).font(.system(size: 28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text1)
                .lineLimit(1).minimumScaleFactor(0.6).padding(.top, 14 * u)
                .enter(.rise, 2, u: u)
            if let m = memos.last {
                note(m, more: memos.count - 1)
                    .padding(.top, 36 * u)
                    .enter(.pop, 3, u: u)
            }
            Spacer(minLength: 24 * u)
            VStack(alignment: .leading, spacing: 9 * u) {
                ForEach(Array(lab.doneSteps(well).prefix(3).enumerated()), id: \.offset) { i, step in
                    Text(["Step \(step)", lab.stepLine(step)].filter { !$0.isEmpty }.joined(separator: " · "))
                        .font(.system(size: 22 * u)).tracking(-0.13 * u).foregroundStyle(Theme.text2)
                        .lineLimit(1).minimumScaleFactor(0.7)
                        .enter(.rise, 4 + i, u: u)
                }
            }
            if let b = buttons.first {
                SayButton(button: b, size: 34 * u, perform: perform)
                    .padding(.top, 36 * u)
                    .modifier(CommandIn(u: u, delay: Theme.Motion.enterStep * 5))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .id(well)  // another well replays the entrance
    }

    /// The latest note as a quote, then its step (and how many more).
    private func note(_ m: Memo, more: Int) -> some View {
        VStack(alignment: .leading, spacing: 18 * u) {
            Text("“\(m.text)”").font(.system(size: 28 * u)).tracking(-0.34 * u)
                .foregroundStyle(Theme.text1).lineLimit(3)
            // "Voice · step 1": how it came in, then when
            let meta = [m.source.map { $0.prefix(1).uppercased() + $0.dropFirst() },
                        m.step.map { (m.source == nil ? "Step " : "step ") + "\($0)" },
                        more > 0 ? "+\(more)" : nil].compactMap { $0 }
            if !meta.isEmpty {
                HStack(spacing: 14 * u) {
                    if let icon = m.source.flatMap({ ["voice": "mic", "tap": "hand.tap", "typed": "keyboard"][$0] }) {
                        Image(systemName: icon).font(.system(size: 20 * u))
                    }
                    Text(meta.joined(separator: " · ")).font(.system(size: 22 * u)).tracking(-0.13 * u)
                }
                .foregroundStyle(Theme.text2)
            }
        }
        .padding(.horizontal, 36 * u).padding(.vertical, 32 * u)
        .frame(maxWidth: .infinity, alignment: .leading)
        .benchCard(radius: 40 * u)
    }
}
