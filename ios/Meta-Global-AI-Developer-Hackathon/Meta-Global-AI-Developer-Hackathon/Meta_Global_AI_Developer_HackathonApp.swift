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
    case splash, connecting, ready

    /// First `ok` rows done, then one pending row, the rest waiting.
    private func rows(ok: Int, pending: String?) -> [SyncRows.Row] {
        let done = ["Allowed", "Connected", "Linked", "Ray-Ban Meta · 82%",
                    "Glasses mic", "Allowed"]
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
                              previewRows: rows(ok: 3, pending: "Searching nearby…"))
        case .ready:
            GlassesSyncScreen(dat: nil, onSkip: {}, onComplete: {},
                              previewRows: rows(ok: 6, pending: nil))
        }
    }
}
#endif
