//
//  Meta_Global_AI_Developer_HackathonApp.swift
//  Meta-Global-AI-Developer-Hackathon
//
//  Created by dongchankim on 10/26/26.
//

import MWDATCore
import os
import SwiftData
import SwiftUI

@main
struct Meta_Global_AI_Developer_HackathonApp: App {
    @State private var dat: GlassesDAT

    init() {
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
        _dat = State(initialValue: d)
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
            ContentView(dat: dat)
        }
        .modelContainer(sharedModelContainer)
    }
}
