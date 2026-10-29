//
//  Meta_Global_AI_Developer_HackathonApp.swift
//  Meta-Global-AI-Developer-Hackathon
//
//  Created by dongchankim on 10/26/26.
//

import CoreBluetooth
import MWDATCore
import os
import SwiftData
import SwiftUI

@main
struct Meta_Global_AI_Developer_HackathonApp: App {
    /// nil until Bluetooth is allowed or the user opens Glasses sync:
    /// configuring DAT starts Bluetooth, which prompts on first use.
    @State private var dat: GlassesDAT?

    init() {
        if CBManager.authorization == .allowedAlways {
            _dat = State(initialValue: Self.makeDAT())
        }
    }

    private static func makeDAT() -> GlassesDAT {
        var configError: String?
        do {
            try Wearables.configure()
        } catch let error as WearablesError {
            configError = error.description
        } catch {
            configError = error.localizedDescription
        }
        Log.dat.info("[dat] configure: \(configError ?? "ok", privacy: .public)")
        let d = GlassesDAT(wearables: Wearables.shared)
        if let configError {
            d.noteError(configError)
        }
        return d
    }

    private func enableDAT() {
        if dat == nil { dat = Self.makeDAT() }
    }

    var sharedModelContainer: ModelContainer = {
        let schema = Schema([
            Item.self,
        ])
        let modelConfiguration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: false)

        do {
            return try ModelContainer(for: schema, configurations: [modelConfiguration])
        } catch {
            fatalError("Could not create ModelContainer: \(error)")
        }
    }()

    var body: some Scene {
        WindowGroup {
            #if DEBUG
            if let preview = DesignPreview(rawValue:
                UserDefaults.standard.string(forKey: "uiPreview") ?? "") {
                preview.view
            } else {
                ContentView(dat: dat, enableDAT: enableDAT)
            }
            #else
            ContentView(dat: dat, enableDAT: enableDAT)
            #endif
        }
        .modelContainer(sharedModelContainer)
    }
}

#if DEBUG
/// Design captures without glasses: launch with `-uiPreview
/// splash|connecting|ready` (optionally `-uiPreviewTime <s>` to freeze).
enum DesignPreview: String {
    case splash, connecting, ready, header

    /// First `ok` rows done, then one pending row, the rest waiting.
    private func rows(ok: Int, pending: String?) -> [SyncRows.Row] {
        let done = ["Allowed", "Connected", "Ray-Ban Meta · 82%", "Glasses mic", "Allowed"]
        return SyncItem.allCases.map { item in
            let i = item.rawValue
            if i < ok { return .init(state: .ok, value: done[i], hint: nil) }
            if i == ok, let pending { return .init(state: .checking, value: pending, hint: nil) }
            return .init(state: .waiting, value: "", hint: nil)
        }
    }

    @ViewBuilder var view: some View {
        switch self {
        case .splash:
            SplashView {}
        case .connecting:
            GlassesSyncScreen(dat: nil, onSkip: {}, onComplete: {},
                              previewRows: rows(ok: 2, pending: "Searching nearby…"))
        case .ready:
            GlassesSyncScreen(dat: nil, onSkip: {}, onComplete: {},
                              previewRows: rows(ok: 5, pending: nil))
        case .header:
            HeaderSamples()
        }
    }
}
/// `-uiPreview header`: the top header with every state and line length
/// (short, one long line, two lines, too long for two) stacked for review.
struct HeaderSamples: View {
    private let lines: [(VoiceSession.State, String, Bool)] = [
        (.ready, "Say “\(Persona.current.wakePhrase)”", false),
        (.listening, "Listening", true),
        (.transcribing, "“Yes”", true),
        (.thinking, "Thinking", true),
        (.speaking, "Nice, C2–C8 are in.", false),
        (.speaking, "Got it — one active run, the Drug A / Drug B dosing protocol from tonight.", false),
        (.speaking, "Step 3 of 5, add 10 µL of Drug A working solution to each Drug A well. Four wells left: C5 to C8, then incubate.", false),
        (.speaking, String(repeating: "This reply is far too long for the header and must stop at two lines. ", count: 4), false),
    ]

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(lines.indices, id: \.self) { i in
                    TopHeaderView(style: Theme.orbStyle(for: lines[i].0), text: lines[i].1, dots: lines[i].2,
                                  you: lines[i].0 == .transcribing,
                                  status: [.listening, .thinking, .searching].contains(lines[i].0),
                                  live: lines[i].0 != .ready,
                                  hearing: [.listening, .transcribing].contains(lines[i].0))
                        .border(Color.black.opacity(0.08))
                }
            }
        }
        .background(Color(.systemBackground)) // the light screens' floor
    }
}
#endif
