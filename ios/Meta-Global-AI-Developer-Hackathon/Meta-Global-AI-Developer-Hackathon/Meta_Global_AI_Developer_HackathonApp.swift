//
//  Meta_Global_AI_Developer_HackathonApp.swift
//  Meta-Global-AI-Developer-Hackathon
//
//  Created by dongchankim on 10/26/26.
//

import SwiftUI
import SwiftData

@main
struct Meta_Global_AI_Developer_HackathonApp: App {
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
            ContentView()
        }
        .modelContainer(sharedModelContainer)
    }
}
