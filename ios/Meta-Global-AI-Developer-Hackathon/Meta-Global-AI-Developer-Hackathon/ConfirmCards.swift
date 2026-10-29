import SwiftUI

// Confirm cards, ver 2 (Figma ③ 37:13714 single, ③b 37:13742 checklist,
// ③c 37:13777 catch, ③d 37:13804 next step): while a card waits, it takes
// the bench. Data: snapshot `cards` (question, detail, kind, payload op /
// wells / target), plan steps for the next-step bars, conditions for the
// well's group. Buttons: registry `cards` (answer words, so tap = voice).
// Server to do: "One by one" for the checklist, a catch title/prompt split.

struct ConfirmScreen: View {
    @Bindable var lab: LabState
    /// Registry `cards`: layout -> answer buttons.
    var buttons: [String: [ScreenButton]]
    /// Design px -> points.
    var u: CGFloat
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void

    /// Newest first, as the server answers a bare "yes".
    private var cards: [ServerCard] { lab.cards.sorted { $0.ts > $1.ts } }
    private var layout: String {
        cards.count > 1 ? "checklist" : cards.first?.kind == "next_step" ? "next_step" : "single"
    }

    var body: some View {
        VStack(spacing: 36 * u) {
            card
                .padding(.horizontal, 64 * u).padding(.vertical, 56 * u)
                .frame(width: 756 * u)
                .benchCard(radius: 44 * u)
                .modifier(ShakeIf(on: cards.first?.kind == "catch" && cards.count == 1, u: u))
                .enter(.pop, u: u)
            HStack(spacing: 36 * u) {
                ForEach(Array((buttons[layout] ?? []).enumerated()), id: \.offset) { i, b in
                    SayButton(button: b, kind: i == 0 ? .primary : .secondary, size: 34 * u, perform: answer)
                        .frame(width: 360 * u)
                        .modifier(EntranceFor(primary: i == 0, order: i + 1, u: u))
                }
            }
            .frame(width: 756 * u, alignment: .leading)
        }
        .id(cards.map(\.id).joined())  // a new card replays the entrance
    }

    @ViewBuilder private var card: some View {
        if cards.count > 1 {
            checklist
        } else if let c = cards.first {
            single(c)
        }
    }

    private func single(_ c: ServerCard) -> some View {
        VStack(spacing: 0) {
            if c.kind == "next_step" {
                Text("Step \(max(1, (c.payload?.target ?? lab.stepIndex + 2) - 1)) done")
                    .font(.system(size: 28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text2)
                    .padding(.bottom, 22 * u)
            } else if c.kind == "catch" {
                Circle().fill(Theme.text1).frame(width: 69 * u, height: 69 * u)
                    .overlay(Image(systemName: "exclamationmark").font(.system(size: 30 * u, weight: .bold))
                        .foregroundStyle(.white))
                    .padding(.bottom, 50 * u)
            } else if let w = c.payload?.allWells.first {
                CardWellIcon(op: c.payload?.op, well: w, lab: lab, d: 72 * u, ring: true)
                    .padding(.bottom, 20 * u)
            }
            Text(c.question)
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
                .lineLimit(2).minimumScaleFactor(0.6)
            if c.kind == "next_step" {
                let target = c.payload?.target ?? lab.stepIndex + 2
                StepBars(count: lab.steps.count, done: target - 1, current: target - 1,
                         rest: Theme.cardOff, u: u)
                    .padding(.top, 30 * u)
            } else if let sub = subline(c) {
                Text(sub).font(.system(size: 28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
                    .lineLimit(2).padding(.top, 24 * u)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// "Drug A · bubble": the well's group, then the card's detail.
    private func subline(_ c: ServerCard) -> String? {
        let group = c.payload?.allWells.first.flatMap { lab.conditionLabel($0) }
        let bits = [group, c.detail].compactMap { $0?.isEmpty == false ? $0 : nil }
        return bits.isEmpty ? nil : bits.joined(separator: " · ")
    }

    private var checklist: some View {
        VStack(alignment: .leading, spacing: 18 * u) {
            Text("\(cards.count) changes")
                .font(.system(size: 64 * u, weight: .semibold)).tracking(-1.79 * u)
                .foregroundStyle(Theme.text1)
                .frame(maxWidth: .infinity)
            ForEach(Array(cards.enumerated()), id: \.element.id) { i, c in
                HStack(spacing: 18 * u) {
                    CardWellIcon(op: c.payload?.op, well: c.payload?.allWells.first, lab: lab,
                                 d: 48 * u, ring: false)
                    Text(c.question.hasSuffix("?") ? String(c.question.dropLast()) : c.question)
                        .font(.system(size: 28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text1)
                        .lineLimit(1).minimumScaleFactor(0.6)
                    Spacer(minLength: 0)
                    Image(systemName: "checkmark").font(.system(size: 22 * u, weight: .semibold))
                        .foregroundStyle(Theme.text2)
                }
                .enter(.text, i + 1, u: u)
            }
        }
    }

    private func answer(_ b: ScreenButton) {
        switch b.args?["answer"] {
        case "all": onAnswerAll()
        case "yes": if let c = cards.first { onAnswer(c.id, true) }
        case "no", "cancel": if let c = cards.first { onAnswer(c.id, false) }
        default: break
        }
    }
}

/// The well a card is about, drawn in the state the change would leave it:
/// excluded (empty, cross), redo (dashed ring, arrow), else its group tone.
struct CardWellIcon: View {
    var op: String?
    var well: String?
    @Bindable var lab: LabState
    var d: CGFloat
    /// The single card's blue focus ring.
    var ring: Bool

    var body: some View {
        ZStack {
            switch op {
            case "exclude_well":
                Circle().fill(Theme.wellEmpty.shadow(.inner(color: .black.opacity(0.07), radius: 1, y: 1)))
                Image(systemName: "xmark").font(.system(size: d * 0.36, weight: .medium))
                    .foregroundStyle(Theme.text2)
            case "record_redo":
                Circle().fill(Theme.card)
                Circle().strokeBorder(Theme.text1, style: StrokeStyle(lineWidth: d / 24, dash: [d / 16, d / 16]))
                Image(systemName: "arrow.clockwise").font(.system(size: d * 0.36, weight: .medium))
                    .foregroundStyle(Theme.text1)
            default:
                Circle().fill(well.flatMap { lab.groupIndex(lab.conditionOf($0)) }.map(Theme.groupTone)
                              ?? Theme.wellEmpty)
            }
        }
        .frame(width: d, height: d)
        .overlay {
            if ring {
                Circle().strokeBorder(Theme.polishBlue, lineWidth: d / 36)
                    .frame(width: d * 1.25, height: d * 1.25)
            }
        }
    }
}

/// The primary command gets cmdIn + cue; the others rise after it.
private struct EntranceFor: ViewModifier {
    var primary: Bool
    var order: Int
    var u: CGFloat

    func body(content: Content) -> some View {
        if primary {
            content.modifier(CommandIn(u: u, delay: Theme.Motion.enterStep * Double(order)))
        } else {
            content.enter(.rise, order, u: u)
        }
    }
}

private struct ShakeIf: ViewModifier {
    var on: Bool
    var u: CGFloat

    func body(content: Content) -> some View {
        if on { content.modifier(ShakeOnce(u: u)) } else { content }
    }
}
