import SwiftUI

// Confirm cards, ver 2 (Figma ③ 37:13714 single, ③b 37:13742 checklist,
// ③c 37:13777 catch, ③d 37:13804 next step): while a card waits, it takes
// the bench. Data: snapshot `cards` (title and detail made by the server
// from pip_voice.json, kind, payload op / wells / target) and plan steps for
// the next-step bars. Buttons: registry `cards` (answer words, so tap =
// voice). Later: "One by one" for the checklist (docs/design/README.md).

struct ConfirmScreen: View {
    @Bindable var lab: LabState
    /// Registry `cards`: layout -> answer buttons.
    var buttons: [String: [ScreenButton]]
    /// Design px -> points.
    var u: CGFloat
    var onAnswer: (String, Bool) -> Void
    var onAnswerAll: () -> Void
    /// A choice card's answer (registry `cards.<kind>`: open / review, version / separate).
    var onChoice: (String, String) -> Void

    /// Newest first, as the server answers a bare "yes".
    private var cards: [ServerCard] { lab.cards.sorted { $0.ts > $1.ts } }
    /// The card kind's own buttons when the registry has them, else single.
    private var layout: String {
        cards.count > 1 ? "checklist" : cards.first.flatMap { buttons[$0.kind] == nil ? nil : $0.kind } ?? "single"
    }

    var body: some View {
        VStack(spacing: 36 * u) {
            card
                .padding(.horizontal, 64 * u).padding(.vertical, 56 * u)
                .frame(width: 756 * u)
                .benchCard(radius: 44 * u)
                .modifier(ShakeIf(on: cards.first?.kind == "catch" && cards.count == 1, u: u))
                .enter(.pop, u: u)
            // big buttons (Figma: two, 360 x 93, 36 apart); a `look: text` answer is a
            // small line under them ("Not yet"), said or tapped the same
            let all = buttons[layout] ?? []
            let row = all.filter { $0.look != "text" }
            let small = all.filter { $0.look == "text" }
            HStack(spacing: 36 * u) {
                ForEach(Array(row.enumerated()), id: \.offset) { i, b in
                    SayButton(button: b, kind: i == 0 ? .primary : .secondary, size: 34 * u, perform: answer)
                        .frame(width: (756 - 36 * CGFloat(max(1, row.count) - 1)) / CGFloat(max(1, row.count)) * u)
                        .modifier(EntranceFor(primary: i == 0, order: i + 1, u: u))
                }
            }
            .frame(width: 756 * u, alignment: .leading)
            // under the row without moving the card and buttons off their Figma place
            .overlay(alignment: .bottom) {
                if !small.isEmpty {
                    HStack(spacing: 36 * u) {
                        ForEach(Array(small.enumerated()), id: \.offset) { i, b in
                            Button { answer(b) } label: {
                                (Say.prefix(Theme.text2) + Text(b.say).fontWeight(.semibold).foregroundStyle(Theme.text1))
                                    .font(Theme.ui(26 * u))
                            }
                            .buttonStyle(.plain)
                            .enter(.rise, row.count + 1 + i, u: u)
                        }
                    }
                    .offset(y: 62 * u)
                }
            }
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
            if let ctx = c.context, !ctx.isEmpty {
                Text(ctx).font(Theme.ui(24 * u)).tracking(-0.3 * u).foregroundStyle(Theme.text2)
                    .lineLimit(1).minimumScaleFactor(0.7)
                    .padding(.bottom, 18 * u)
            }
            if c.kind == "next_step", let d = c.detail, !d.isEmpty {
                Text(d).font(Theme.ui(28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text2)
                    .padding(.bottom, 22 * u)
            } else if c.kind == "catch" {
                Circle().fill(Theme.text1).frame(width: 69 * u, height: 69 * u)
                    .overlay(Image(systemName: "exclamationmark").font(Theme.ui(30 * u, weight: .bold))
                        .foregroundStyle(.white))
                    .padding(.bottom, 50 * u)
            } else if let w = c.payload?.allWells.first {
                CardWellIcon(op: c.payload?.op, well: w, lab: lab, d: 72 * u, ring: true)
                    .padding(.bottom, 20 * u)
            }
            Text(c.title ?? c.question)
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
                .lineLimit(2).minimumScaleFactor(0.6)
            if c.kind == "next_step" {
                let target = c.payload?.target ?? lab.stepIndex + 2
                StepBars(count: lab.steps.count, done: target - 1, current: target - 1,
                         rest: Theme.cardOff, u: u)
                    .padding(.top, 30 * u)
            } else if let sub = c.detail, !sub.isEmpty {
                Text(sub).font(Theme.ui(28 * u)).tracking(-0.34 * u)
                    .foregroundStyle(Theme.text1).multilineTextAlignment(.center)
                    .lineLimit(4).multilineTextAlignment(.center).padding(.top, 24 * u)
            }
        }
        .frame(maxWidth: .infinity)
        // ③c's card height (Figma 37:13777): the not-marked card sits where the catch card does
        .frame(minHeight: c.kind == "unmarked" ? (360 - 2 * 56) * u : 0)
    }

    private var checklist: some View {
        VStack(alignment: .leading, spacing: 18 * u) {
            Text("\(cards.count) changes")
                .font(Theme.ui(64 * u, weight: .semibold)).tracking(-1.79 * u).displayLine(u)
                .foregroundStyle(Theme.text1)
                .frame(maxWidth: .infinity)
            ForEach(Array(cards.enumerated()), id: \.element.id) { i, c in
                HStack(spacing: 18 * u) {
                    CardWellIcon(op: c.payload?.op, well: c.payload?.allWells.first, lab: lab,
                                 d: 48 * u, ring: false)
                    let t = c.title ?? c.question
                    Text(t.hasSuffix("?") ? String(t.dropLast()) : t)
                        .font(Theme.ui(28 * u)).tracking(-0.34 * u).foregroundStyle(Theme.text1)
                        .lineLimit(1).minimumScaleFactor(0.6)
                    Spacer(minLength: 0)
                    Image(systemName: "checkmark").font(Theme.ui(22 * u, weight: .semibold))
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
        case let a?: if let c = cards.first { onChoice(c.id, a) }
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
                Image(systemName: "xmark").font(Theme.ui(d * 0.36, weight: .medium))
                    .foregroundStyle(Theme.text2)
            case "record_redo":
                Circle().fill(Theme.card)
                Circle().strokeBorder(Theme.text1, style: StrokeStyle(lineWidth: d / 24, dash: [d / 16, d / 16]))
                Image(systemName: "arrow.clockwise").font(Theme.ui(d * 0.36, weight: .medium))
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
