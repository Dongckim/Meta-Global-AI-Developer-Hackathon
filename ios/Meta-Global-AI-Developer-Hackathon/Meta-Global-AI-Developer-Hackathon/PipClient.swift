import Foundation

enum Config {
    /// From Secrets.xcconfig via Info.plist (INFOPLIST_KEY_ServerBaseURL).
    static var baseURL: URL {
        let raw = Bundle.main.infoDictionary?["ServerBaseURL"] as? String
        return URL(string: raw ?? "") ?? URL(string: "http://localhost:8000")!
    }
}

struct ToolCall: Decodable {
    let name: String
    let args: [String: JSON]
}

struct SavedMemo: Decodable {
    let id: String
    let target: String
    let text: String
}

struct Source: Decodable {
    let title: String
    let url: String
}

struct OrchestrateResponse: Decodable {
    let tool_calls: [ToolCall]
    let messages: [String]
    let reply: String
    let ms: [String: Int]
    let route: String?
    let cards: [ServerCard]
    let record_version: Int
    let sources: [Source]
    let saved_memo: SavedMemo?
    let robot: Bool?
    let dropped: Int?
}

struct VoiceResponse: Decodable {
    let transcript: String
    let tool_calls: [ToolCall]
    let messages: [String]
    let reply: String
    let ms: [String: Int]
    let route: String?
    let cards: [ServerCard]
    let record_version: Int
    let sources: [Source]
    let saved_memo: SavedMemo?
    let robot: Bool?
    let dropped: Int?
}

struct ConfirmResponse: Decodable {
    let message: String
    let cards: [ServerCard]
    let record_version: Int
}

struct EditNoteResponse: Decodable {
    let message: String
    let record_version: Int
}

struct RobotCheck: Decodable {
    let expected: [String]
    let simulated: [String]
    let missing: [String]
    let extra: [String]
    let ok: Bool
}

struct RobotSim: Decodable {
    let ok: Bool
    let exit: Int?
    let log: String
}

struct RobotPreview: Decodable {
    let csv_head: String
    let code_head: String
}

struct RobotResponse: Decodable {
    let csv: String
    let code: String
    let sim: RobotSim
    let check: RobotCheck
    let preview: RobotPreview
}

enum SSEvent {
    case snapshot(RecordSnapshot)
    case timerWarning(label: String)
    case timerDone(message: String)
}

enum PipError: Error, LocalizedError {
    case server(Int, String)
    var errorDescription: String? {
        switch self {
        case .server(let code, let body): "Server error (\(code)) \(body.prefix(120))"
        }
    }
}

enum PipClient {
    static func orchestrate(text: String) async throws -> OrchestrateResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "orchestrate"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(OrchestrateResponse.self, from: data)
    }

    static func voice(wav: Data) async throws -> VoiceResponse {
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n")
        append("Content-Type: audio/wav\r\n\r\n")
        body.append(wav)
        append("\r\n--\(boundary)--\r\n")

        var req = URLRequest(url: Config.baseURL.appending(path: "voice"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(VoiceResponse.self, from: data)
    }

    static func confirm(cardId: String, ok: Bool) async throws -> ConfirmResponse {
        try await post("confirm", ["card_id": cardId, "ok": ok])
    }

    static func editNote(id: String, text: String) async throws -> EditNoteResponse {
        try await post("notes/edit", ["id": id, "text": text])
    }

    static func record() async throws -> RecordSnapshot {
        let (data, resp) = try await URLSession.shared.data(from: Config.baseURL.appending(path: "record"))
        try check(resp, data)
        return try JSONDecoder().decode(RecordSnapshot.self, from: data)
    }

    static func robot() async throws -> RobotResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "robot"))
        req.httpMethod = "POST"
        req.timeoutInterval = 400
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(RobotResponse.self, from: data)
    }

    /// Server-sent events: snapshots and timer pushes. Runs until cancelled.
    static func events() -> AsyncThrowingStream<SSEvent, Error> {
        AsyncThrowingStream { cont in
            let task = Task {
                var req = URLRequest(url: Config.baseURL.appending(path: "events"))
                req.timeoutInterval = 3600
                let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                    throw PipError.server((resp as? HTTPURLResponse)?.statusCode ?? -1, "")
                }
                for try await line in bytes.lines {
                    guard line.hasPrefix("data:") else { continue }
                    let payload = Data(line.dropFirst(5).trimmingCharacters(in: .whitespaces).utf8)
                    guard let obj = try? JSONDecoder().decode([String: JSON].self, from: payload),
                          let kind = obj["kind"]?.string
                    else { continue }
                    switch kind {
                    case "snapshot":
                        if let snap = try? JSONDecoder().decode(RecordSnapshot.self, from: payload) {
                            cont.yield(.snapshot(snap))
                        }
                    case "timer_warning":
                        if let label = obj["label"]?.string { cont.yield(.timerWarning(label: label)) }
                    case "timer_done":
                        if let msg = obj["message"]?.string { cont.yield(.timerDone(message: msg)) }
                    default: break
                    }
                }
                cont.finish()
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }

    private static func post<T: Decodable>(_ path: String, _ json: [String: Any]) async throws -> T {
        var req = URLRequest(url: Config.baseURL.appending(path: path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: json)
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            throw PipError.server(code, String(data: data, encoding: .utf8) ?? "")
        }
    }
}
