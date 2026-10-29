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

struct UIAction: Decodable {
    let action: String
    let screen: String?
    let target: String?
    let sheet: String?
    let wells: [String]?
    let seconds: Double?
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
    let ui: [UIAction]?
    let speak: String?
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
    let ui: [UIAction]?
    let speak: String?
    let ignored: Bool?
    let setup_done: Bool?
    let sync_retry: Bool?
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

struct ImportResponse: Decodable {
    let draft: ProtocolDraft
    let review: [DraftIssue]
    let errors: [DraftIssue]
    let reply: String
    let speak: String?
}

struct PatchResponse: Decodable {
    let ok: Bool
    let draft: ProtocolDraft?
    let review: [DraftIssue]?
    let errors: [DraftIssue]?
    let reason: String?
}

struct StartResponse: Decodable {
    let started: Bool
    let errors: [DraftIssue]?
    let card: ServerCard?
    let runId: String?
    let protocolId: String?
    let protocolName: String?
    let reply: String
    let speak: String?

    enum CodingKeys: String, CodingKey {
        case started, errors, card, runId = "run_id", protocolId = "protocol_id",
             protocolName = "protocol_name", reply, speak
    }
}

struct SaveResponse: Decodable {
    let saved: Bool
    let proto: ProtocolSummary?
    let errors: [DraftIssue]?
    let reply: String
    let speak: String?

    enum CodingKeys: String, CodingKey {
        case saved, proto = "protocol", errors, reply, speak
    }
}

struct RunStartResponse: Decodable {
    let started: Bool
    let card: ServerCard?
    let runId: String?
    let protocolId: String?
    let reply: String
    let speak: String?

    enum CodingKeys: String, CodingKey {
        case started, card, runId = "run_id", protocolId = "protocol_id", reply, speak
    }
}

struct RunEndResponse: Decodable {
    let ended: Bool
    let card: ServerCard?
    let reply: String
    let speak: String?
}

struct ProtocolListResponse: Decodable {
    let protocols: [ProtocolSummary]
}

struct ProtocolDetailResponse: Decodable {
    let proto: ProtocolDoc

    enum CodingKeys: String, CodingKey {
        case proto = "protocol"
    }
}

struct RunListResponse: Decodable {
    let runs: [RunSummary]
}

struct RunDetailResponse: Decodable {
    let run: RunDoc
    let label: String?
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
    case timerWarning(message: String, speak: String?)
    case timerDone(message: String, speak: String?)
    case ui(UIAction)
    case broken(String)
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
    /// Must match server API_VERSION (test_snapshot_schema pins both sides).
    static let EXPECTED_API_VERSION = 9
    /// A server link silent this long is dead: three missed keepalives
    /// (server HEARTBEAT_S = 15) on /events and /voice/live.
    static let idleTimeout: TimeInterval = 45

    /// /voice/live on the same server (http -> ws, https -> wss).
    static var liveURL: URL {
        var c = URLComponents(url: Config.baseURL.appending(path: "voice/live"), resolvingAgainstBaseURL: false)!
        c.scheme = c.scheme == "https" ? "wss" : "ws"
        return c.url!
    }

    /// Below this clip length nothing is sent (single choke point for all
    /// mic buttons; the server additionally ignores zero-frame audio).
    static let minVoiceDuration = 0.3

    enum VoiceError: Error {
        case tooShort(duration: Double)
    }

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
        if let dur = Recorder.duration(of: wav), dur < minVoiceDuration {
            throw VoiceError.tooShort(duration: dur)
        }
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        func field(_ name: String, _ value: String) {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            append("\(value)\r\n")
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n")
        append("Content-Type: audio/wav\r\n\r\n")
        body.append(wav)
        append("\r\n")
        append("--\(boundary)--\r\n")

        var req = URLRequest(url: Config.baseURL.appending(path: "voice"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(VoiceResponse.self, from: data)
    }

    static func inputAction(id: String, args: [String: Any] = [:]) async throws -> OrchestrateResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "input"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "action": ["id": id, "args": args],
        ])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(OrchestrateResponse.self, from: data)
    }

    static func editNote(id: String, text: String) async throws -> EditNoteResponse {
        try await post("notes/edit", ["id": id, "text": text])
    }

    static func importProtocol(data: Data, filename: String) async throws -> ImportResponse {
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".utf8))
        body.append(Data("Content-Type: application/octet-stream\r\n\r\n".utf8))
        body.append(data)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: Config.baseURL.appending(path: "protocols/import"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (respData, resp) = try await URLSession.shared.data(for: req)
        try check(resp, respData)
        return try JSONDecoder().decode(ImportResponse.self, from: respData)
    }

    static func patchDraft(path: String, value: Any) async throws -> PatchResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "protocols/draft"))
        req.httpMethod = "PATCH"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["path": path, "value": value])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(PatchResponse.self, from: data)
    }

    static func startRun() async throws -> StartResponse {
        try await post("protocols/start", [:])
    }

    static func saveDraft() async throws -> SaveResponse {
        try await post("protocols/save", [:])
    }

    static func protocols() async throws -> [ProtocolSummary] {
        let r: ProtocolListResponse = try await get("protocols")
        return r.protocols
    }

    static func protocolDetail(id: String) async throws -> ProtocolDoc {
        let r: ProtocolDetailResponse = try await get("protocols/\(id)")
        return r.proto
    }

    static func runsStart(protocolId: String) async throws -> RunStartResponse {
        try await post("runs/start", ["protocol_id": protocolId])
    }

    static func runsEnd() async throws -> RunEndResponse {
        try await post("runs/end", [:])
    }

    static func runs(protocolId: String? = nil) async throws -> [RunSummary] {
        var url = Config.baseURL.appending(path: "runs")
        if let p = protocolId {
            url = url.appending(queryItems: [URLQueryItem(name: "protocol_id", value: p)])
        }
        let (data, resp) = try await URLSession.shared.data(from: url)
        try check(resp, data)
        return try JSONDecoder().decode(RunListResponse.self, from: data).runs
    }

    static func runDetail(id: String) async throws -> RunDetailResponse {
        try await get("runs/\(id)")
    }

    static func health() async throws -> HealthInfo {
        try await get("health")
    }

    static func discardDraft() async throws {
        var req = URLRequest(url: Config.baseURL.appending(path: "protocols/draft"))
        req.httpMethod = "DELETE"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [:])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
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
                req.timeoutInterval = idleTimeout  // idle gap between bytes, not total time
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
                        do {
                            cont.yield(.snapshot(try JSONDecoder().decode(RecordSnapshot.self, from: payload)))
                        } catch {
                            cont.yield(.broken(syncReason(error)))
                        }
                    case "timer_warning":
                        let msg = obj["message"]?.string ?? "⏰ \(obj["label"]?.string ?? "timer"): 5 minutes left"
                        cont.yield(.timerWarning(message: msg, speak: obj["speak"]?.string))
                    case "timer_done":
                        if let msg = obj["message"]?.string {
                            cont.yield(.timerDone(message: msg, speak: obj["speak"]?.string))
                        }
                    case "ui":
                        if let act = try? JSONDecoder().decode(UIAction.self, from: payload) {
                            cont.yield(.ui(act))
                        }
                    default: break
                    }
                }
                cont.finish()
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }

    private static func get<T: Decodable>(_ path: String) async throws -> T {
        let (data, resp) = try await URLSession.shared.data(from: Config.baseURL.appending(path: path))
        try check(resp, data)
        return try JSONDecoder().decode(T.self, from: data)
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
