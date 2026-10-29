import SwiftUI

/// Design tokens from docs/design/benchtrace-pip-v22.html (`:root`,
/// `.orb`/`.pip`, aurora palettes, keyframes). Screens use these tokens;
/// no literal design color, font, or duration in views.
///
/// Deviations from the HTML are marked with "diff:" and explained in the
/// slice report. Inter is unavailable, so all UI type is SF Pro (the
/// platform system font); mono is SF Mono via `.monospaced()`.
enum Theme {
    // MARK: - Canvas

    /// --chrome-bg
    static let chromeBg = Color(hex: 0x0B0B0C)
    /// --chrome-fg
    static let chromeFg = Color(hex: 0xEDEDEF)
    /// --chrome-mut
    static let chromeMut = Color(hex: 0x8E8E93)
    /// --chrome-line
    static let chromeLine = Color(hex: 0x26262A)
    /// --chrome-chip
    static let chromeChip = Color(hex: 0x18181B)
    /// --s-bg (sync canvas)
    static let syncBg = Color(hex: 0xD4D4D4)
    /// --card
    static let card = Color(hex: 0xFFFFFF)
    /// --ink
    static let ink = Color(hex: 0x0A0A0A)
    /// --black
    static let black = Color(hex: 0x000000)
    /// --blue (accent)
    static let blue = Color(hex: 0x0090FF)
    /// --dark (polish layer; the base sheet's #232324 is overridden)
    static let dark = Color(hex: 0x1C1C1E)
    /// --blue in the polish layer (sync tick); `blue` keeps the base value
    /// the orb gradient was drawn with.
    static let polishBlue = Color(hex: 0x0071E3)
    /// --sub
    static let sub = Color(hex: 0xEDEDED)
    /// --track
    static let track = Color(hex: 0xC9C9C9)
    /// --on (text on dark)
    static let on = Color(hex: 0xFFFFFF)

    // MARK: - Muted grays

    /// --mut-t (muted text)
    static let mutT = Color(hex: 0x9A9A9A)
    /// --mut-say
    static let mutSay = Color(hex: 0x7C7C7C)
    /// --mut-w (muted on white)
    static let mutW = Color(hex: 0x5E5E5E)
    /// --mut-d (muted on dark)
    static let mutD = Color(hex: 0xA2A2A2)
    /// --icon-w / --icon-g
    static let iconW = Color(hex: 0xDFDFDF)
    static let iconG = Color(hex: 0xC1C1C1)

    // MARK: - Orb gradient (`.orb::before`)

    /// radial-gradient(circle at 50% 52%, #fff 0 9%, #F6FFA9 20%,
    /// #8CC6FF 42%, rgba(104,237,255,.55) 56%, rgba(0,144,255,.28) 66%,
    /// transparent 74%)
    static let orbStops: [Gradient.Stop] = [
        .init(color: .white, location: 0.0),
        .init(color: .white, location: 0.09),
        .init(color: Color(hex: 0xF6FFA9), location: 0.20),
        .init(color: Color(hex: 0x8CC6FF), location: 0.42),
        .init(color: Color(hex: 0x68EDFF).opacity(0.55), location: 0.56),
        .init(color: blue.opacity(0.28), location: 0.66),
        .init(color: .clear, location: 0.74),
    ]
    /// Speaking orb: hollow ring (transparent center, blue rim).
    static let orbSpeakingStops: [Gradient.Stop] = [
        .init(color: .clear, location: 0.0),
        .init(color: .clear, location: 0.30),
        .init(color: Color(hex: 0x5AC8FA).opacity(0.5), location: 0.44),
        .init(color: Color(hex: 0x0071E3).opacity(0.42), location: 0.56),
        .init(color: Color(hex: 0x0071E3).opacity(0.16), location: 0.66),
        .init(color: .clear, location: 0.74),
    ]
    /// `.orb::after` ring: 2px solid rgba(0,144,255,.35).
    static let orbRing = blue.opacity(0.35)

    // MARK: - Aurora palettes (design JS: BLOBS + PIP voice / blue maps)

    /// Default blob colors (gray base + ice blues + warm accents).
    static let auroraDefault: [Color] = [
        Color(hex: 0x525252), Color(hex: 0x8CE2FF), Color(hex: 0x8CB6FF),
        Color(hex: 0x8CC6FF), Color(hex: 0x70C1FF), Color(hex: 0x84A7B2),
        Color(hex: 0x68EDFF), Color(hex: 0xF6FFA9), Color(hex: 0xD5DC99),
    ]
    /// Speaking aurora (PIP_VOICE map): violet shift.
    static let auroraVoice: [Color] = [
        Color(hex: 0x525252), Color(hex: 0xB4B8FF), Color(hex: 0x9C9CFF),
        Color(hex: 0xA7A8FF), Color(hex: 0x8C88FF), Color(hex: 0x9E9AB8),
        Color(hex: 0xD7A8FF), Color(hex: 0xFFE6C4), Color(hex: 0xF2CDA8),
    ]
    /// Splash + sync aurora (BLUE map): saturated blues.
    static let auroraBlue: [Color] = [
        Color(hex: 0x525252), Color(hex: 0x5AC8FA), Color(hex: 0x2E8BFF),
        Color(hex: 0x4DA3FF), Color(hex: 0x0071E3), Color(hex: 0x3B6FA8),
        Color(hex: 0x7FD3FF), Color(hex: 0x7CC4FF), Color(hex: 0x5AA8F0),
    ]

    // MARK: - Type (Inter -> SF Pro; sizes in design px at 1376 wide)

    static func ui(_ px: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: px, weight: weight)
    }
    /// .t60 (polish layer: 400 52px/1.1, -.022em); sizes are design px,
    /// used inside `DarkCanvas`, which scales the 1376x1032 canvas.
    static let t60Size: CGFloat = 52
    static let t60Line: CGFloat = 52 * 1.1
    static let t60Tracking: CGFloat = -0.022 * 52
    /// .t30 (polish layer: 400 28px/1.32, -.012em).
    static let t30 = Font.system(size: 28, weight: .regular)
    static let t30Line: CGFloat = 28 * 1.32
    static let t30Tracking: CGFloat = -0.012 * 28
    /// .t24 (polish layer: 400 22px/1.36, -.006em).
    static let t24 = Font.system(size: 22, weight: .regular)
    static let t24Line: CGFloat = 22 * 1.36
    static let t24Tracking: CGFloat = -0.006 * 22
    /// .pip top line: 300 30px. diff: design canvas size; the shipped top
    /// line caps at 17pt via `pipLine(in:)` so it fits the app bar.
    static func pipLine(in width: CGFloat) -> Font {
        .system(size: min(30, px(30, in: width)), weight: .light)
    }
    /// Monospace (JetBrains Mono -> SF Mono).
    static func mono(_ px: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: px, weight: weight, design: .monospaced)
    }

    // MARK: - Motion (--ease / --spring + keyframe durations)

    /// --ease cubic-bezier(.2,.8,.2,1).
    static func ease(duration: Double) -> Animation {
        .timingCurve(0.2, 0.8, 0.2, 1, duration: duration)
    }
    /// --spring cubic-bezier(.34,1.56,.64,1).
    static func spring(duration: Double = 0.35) -> Animation {
        .timingCurve(0.34, 1.56, 0.64, 1, duration: duration)
    }

    /// breathe 3.2s (orb), .6s listening, 1.1s thinking.
    static let breathe: Double = 3.2
    static let breatheListening: Double = 0.6
    static let breatheThinking: Double = 1.1
    /// orbRing 3.2s (.9s listening), ring 2.4s scale target.
    static let orbRingPeriod: Double = 3.2
    static let orbRingListening: Double = 0.9
    /// readyBreath 5s, pulse 1.6s, think 4s, sweep 2.4s.
    static let readyBreath: Double = 5
    static let pulse: Double = 1.6
    static let think: Double = 4
    static let sweep: Double = 2.4
    /// splashBloom 2.4s, syncFlow 6s, readyGlow 1.2s, spin .8s.
    static let splashBloom: Double = 2.4
    static let syncFlow: Double = 6
    static let readyGlow: Double = 1.2
    static let spin: Double = 0.8

    /// Largest orb size factor (listening 1.32 x breathe 1.08): the header
    /// reserves this slot so the orb never pushes or covers the line.
    static let orbMaxScale: CGFloat = 1.32 * 1.08

    /// Header mode label: the app has one mode (cloud + web search = "Full").
    static let modeLabel = "Full"

    // MARK: - Voice edge glow (design `.edge`, Apple-Intelligence style)

    /// conic-gradient(#0071E3, #1E90FF, #5AC8FA, #B9ECFF, #5AC8FA, #1E90FF, #0071E3)
    static let edgeListening: [Color] = [
        Color(hex: 0x0071E3), Color(hex: 0x1E90FF), Color(hex: 0x5AC8FA), Color(hex: 0xB9ECFF),
        Color(hex: 0x5AC8FA), Color(hex: 0x1E90FF), Color(hex: 0x0071E3),
    ]
    /// Speaking: the ②-5 voice (violet) aurora palette, same mirrored order.
    static let edgeSpeaking: [Color] = [
        Color(hex: 0x8C88FF), Color(hex: 0x9C9CFF), Color(hex: 0xB4B8FF), Color(hex: 0xD7A8FF),
        Color(hex: 0xB4B8FF), Color(hex: 0x9C9CFF), Color(hex: 0x8C88FF),
    ]
    /// Design px at 1376, radius 32. By iPad review the soft band is thicker
    /// than the design's (blur 16 / band 18) and its crisp 4 px line is left out.
    static let edgeSoftBlur: CGFloat = 20
    static let edgeSoftBand: CGFloat = 30
    static let edgeRadius: CGFloat = 32
    static let edgeSpin: Double = 2.6
    static let edgeFade: Double = 0.5
    /// A live session's close fades slower.
    static let edgeSessionFade: Double = 1.0
    /// Top line while a live session is open and nobody talks.
    static let sessionIdleLine = "Listening…"

    struct EdgeStyle: Equatable {
        var colors: [Color]
        /// Seconds per turn; nil = no rotation (Reduce Motion).
        var spin: Double?
        /// Opacity multiplier (brightness).
        var brightness: Double
        /// Band width multiplier.
        var thickness: CGFloat
    }

    /// Edge glow from voice state only. `level` = smoothed mic RMS, `pulse`
    /// 0...1 decaying from the last TTS word. nil = not drawn (ready).
    static func edgeStyle(for state: VoiceSession.State, level: Float, pulse: Double,
                          reduceMotion: Bool, session: Bool = false) -> EdgeStyle? {
        var s: EdgeStyle
        switch (state, session) {
        case (.ready, _):
            return nil
        case (.listening, true):
            // session idle: open but quiet — dim and slow
            s = EdgeStyle(colors: edgeListening, spin: edgeSpin * 3, brightness: 0.3, thickness: 0.8)
        case (.listening, false), (.transcribing, true):
            // someone is talking (in a session, partials mean speech)
            let n = CGFloat(min(1, max(0, level / 0.12)))
            s = EdgeStyle(colors: edgeListening, spin: edgeSpin,
                          brightness: 0.8 + 0.2 * Double(n), thickness: 1 + 0.6 * n)
        case (.transcribing, false), (.thinking, _), (.searching, _):
            s = EdgeStyle(colors: edgeListening, spin: edgeSpin * 2, brightness: 0.55, thickness: 1)
        case (.speaking, _):
            let p = min(1, max(0, pulse))
            s = EdgeStyle(colors: edgeSpeaking, spin: edgeSpin,
                          brightness: 0.8 + 0.2 * p, thickness: 1 + 0.35 * CGFloat(p))
        }
        if reduceMotion { s.spin = nil }
        return s
    }

    /// Word pulse: 1 at a TTS word boundary, decaying with a 0.18 s constant.
    static func wordPulse(since lastWord: Date, now: Date) -> Double {
        let dt = now.timeIntervalSince(lastWord)
        return dt < 0 ? 0 : exp(-dt / 0.18)
    }

    // MARK: - Proportionate sizing (design canvas 1376 x 1032)

    /// Design width in px; sizes scale linearly with the real width.
    static let canvasWidth: CGFloat = 1376
    static let canvasHeight: CGFloat = 1032
    static func px(_ designPx: CGFloat, in width: CGFloat) -> CGFloat {
        guard width > 0 else { return designPx }
        return designPx * width / canvasWidth
    }

    // MARK: - Voice state -> orb style (fullscreen Pip screens, shrunk)

    /// Render recipe for the top orb + aurora, from st-ready/listening/
    /// transcribing/thinking/speaking/searching. Pure; unit-tested.
    struct OrbStyle: Equatable {
        /// Orb breathe period (nil = still).
        var breathe: Double?
        /// Ring period (nil = no ring).
        var ring: Double?
        /// Orb scale (1 = rest).
        var scale: CGFloat
        /// Aurora recipe.
        var aurora: Aurora
        /// Speaking hollow-ring orb.
        var hollow: Bool

        enum Aurora: Equatable {
            /// readyBreath 5s.
            case breath
            /// pulse 1.6s.
            case pulse
            /// Static scale(1.15).
            case still(scale: CGFloat)
            /// think 4s, opacity .7.
            case think
            /// scale(1.24 + amp * .16), voice palette.
            case voice(amp: Double)
            /// sweep 2.4s.
            case sweep
        }
    }

    static func orbStyle(for state: VoiceSession.State) -> OrbStyle {
        switch state {
        case .ready:
            return OrbStyle(breathe: breathe, ring: orbRingPeriod,
                            scale: 1, aurora: .breath, hollow: false)
        case .listening:
            return OrbStyle(breathe: breatheListening,
                            ring: orbRingListening,
                            scale: 1.32, aurora: .pulse, hollow: false)
        case .transcribing:
            // diff: fullscreen has no orb motion spec; hold the listening
            // shape at rest scale so the partial-text line stays readable.
            return OrbStyle(breathe: nil, ring: nil,
                            scale: 1, aurora: .still(scale: 1.15),
                            hollow: false)
        case .thinking:
            return OrbStyle(breathe: breatheThinking, ring: nil,
                            scale: 1.12, aurora: .think, hollow: false)
        case .speaking:
            return OrbStyle(breathe: nil, ring: nil,
                            scale: 1, aurora: .voice(amp: 0),
                            hollow: true)
        case .searching:
            return OrbStyle(breathe: nil, ring: nil,
                            scale: 1, aurora: .sweep, hollow: false)
        }
    }

    // MARK: - Voice state -> Pip line (fullscreen Pip copy, shrunk)

    /// Top-line copy from the ②-1..②-6 screens. `speakText` is the last
    /// Pip reply (speaking shows it verbatim); `partialText` is the live
    /// transcript when slice B streams it (nil until then).
    struct PipLine: Equatable {
        var text: String
        /// Trailing animated dots (.pip .dots).
        var dots: Bool
    }

    /// Ready line when "Hey Pip" can't run: nil = it can. Permission
    /// problems point at the sync screen (the line opens it).
    static func talkBlocker(wake: WakeListener.Support, wakeOn: Bool) -> String? {
        switch wake {
        case .undetermined, .micDenied, .speechDenied, .speechRestricted:
            return "Allow mic to talk to \(Persona.current.agentName)"
        case .noRecognizer, .noOnDevice:
            return "Hold the mic to talk to \(Persona.current.agentName)"
        case .ok:
            return wakeOn ? nil : "Hold the mic to talk to \(Persona.current.agentName)"
        }
    }

    static func pipText(for state: VoiceSession.State, speakText: String,
                        partialText: String?, wake: WakeListener.Support = .ok,
                        wakeOn: Bool = true) -> PipLine {
        switch state {
        case .ready:
            if let blocked = talkBlocker(wake: wake, wakeOn: wakeOn) {
                return PipLine(text: blocked, dots: false)
            }
            // diff: design says "Say <name>" but the trigger is the wake phrase.
            return PipLine(text: "Say “\(Persona.current.wakePhrase)”", dots: false)
        case .listening:
            return PipLine(text: "\(Persona.current.agentName) Listening", dots: false)
        case .transcribing:
            if let partialText, !partialText.isEmpty {
                return PipLine(text: partialText, dots: false)
            }
            return PipLine(text: "Transcribing", dots: true)
        case .thinking:
            return PipLine(text: "\(Persona.current.agentName) Thinking", dots: true)
        case .speaking:
            if !speakText.isEmpty {
                return PipLine(text: speakText, dots: false)
            }
            return PipLine(text: "Speaking", dots: true)
        case .searching:
            return PipLine(text: "\(Persona.current.agentName) Searching", dots: true)
        }
    }

    // MARK: - Sync row state -> style (.srow/.sico)

    enum SyncRow: Equatable {
        case checking
        case ok
        case failed
        /// Blocked on another row (design pending look).
        case waiting
        /// Needs the user's tap (e.g. "Tap to allow"): hollow, full opacity.
        case action
    }

    /// (row opacity, icon style). `.srow` rests at opacity .38 and lights
    /// to 1 while checking (.on) or done (.ok); `.sico` spins / fills
    /// white with a blue tick.
    struct SyncStyle: Equatable {
        var rowOpacity: Double
        var icon: SyncIcon
        enum SyncIcon: Equatable {
            case spin
            case tick
            case hollow
        }
    }

    static func syncStyle(for row: SyncRow) -> SyncStyle {
        switch row {
        case .checking:
            return SyncStyle(rowOpacity: 1, icon: .spin)
        case .ok:
            return SyncStyle(rowOpacity: 1, icon: .tick)
        case .failed, .waiting:
            // diff: the design has no failure; failed rows use the
            // pending look (dim, hollow) with a short reason as the value.
            return SyncStyle(rowOpacity: 0.38, icon: .hollow)
        case .action:
            // diff: the design has no tap-to-ask row; lit like .on so the
            // next step reads, hollow because nothing is running.
            return SyncStyle(rowOpacity: 1, icon: .hollow)
        }
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}
