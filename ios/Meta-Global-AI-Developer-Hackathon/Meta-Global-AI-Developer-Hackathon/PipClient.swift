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
    /// control: a page number ("page 2").
    let n: Int?
    /// Delivery id: the reply and /events carry the same one; run it once.
    let id: String?
    /// camera: the photo question it's for (Retake sends it again).
    let search_id: String?
    /// share: the files the iPad share sheet sends (name to save as, server path).
    let files: [ShareFile]?
}

struct ShareFile: Decodable {
    let name: String
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
    /// nil when the original is already a protocol (the server opens it instead).
    let draft: ProtocolDraft?
    let review: [DraftIssue]
    let errors: [DraftIssue]
    let reply: String
    let speak: String?
}

struct LookResponse: Decodable {
    let ok: Bool
    let reply: String
    let speak: String?
    let recordVersion: Int?
    /// For a photo Retake replaced: show and say nothing.
    let stale: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, reply, speak, stale, recordVersion = "record_version"
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

struct RunListResponse: Decodable {
    let runs: [RunSummary]
}

/// GET /searches: answered web searches (newest first) and how many were sent.
struct SearchListResponse: Decodable {
    let searches: [WebSearch]
    let sent: Int
}

struct RunDetailResponse: Decodable {
    let run: RunDoc
    let label: String?
    /// The robot code the run became, step by step (nil until it passed).
    let handoff: RunCode?
    /// Server: the run has robot code (no Code tab without it).
    let has_code: Bool?
}

/// A finished run's protocol.py and where each final protocol step sits in
/// it (server store.handoff_code): 1-based line ranges.
struct RunCode: Decodable, Equatable {
    let code: String
    let setup: [Int]
    let steps: [Step]
    let exported: Bool

    struct Step: Decodable, Equatable {
        let n: Int
        let title: String
        let detail: String
        let plan: String
        let changes: [String]
        /// [first, last] line of protocol.py ("Step N" comment to the next step).
        let lines: [Int]?
        /// The step's liquid color in the Opentrons App (gray for a wait).
        let color: String
    }
}

enum SSEvent {
    case snapshot(RecordSnapshot)
    case timerWarning(message: String, speak: String?)
    case timerDone(message: String, speak: String?)
    case ui(UIAction)
    case stage(TurnStage)
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
    static let EXPECTED_API_VERSION = 29
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

    /// Private: speech turns to text on the iPad (snapshot `transcribe`).
    static var transcribeOnDevice = false

    static func voice(wav: Data) async throws -> VoiceResponse {
        if let dur = Recorder.duration(of: wav), dur < minVoiceDuration {
            throw VoiceError.tooShort(duration: dur)
        }
        let heard = transcribeOnDevice ? try await DeviceEars.transcribe(wav: wav) : nil
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        func field(_ name: String, _ value: String) {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            append("\(value)\r\n")
        }
        if let heard {
            field("transcript", heard)
        } else {
            append("--\(boundary)\r\n")
            append("Content-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n")
            append("Content-Type: audio/wav\r\n\r\n")
            body.append(wav)
            append("\r\n")
        }
        append("--\(boundary)--\r\n")

        var req = URLRequest(url: Config.baseURL.appending(path: "voice"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(VoiceResponse.self, from: data)
    }

    /// A tapped action: the server answers it on screen only (no `speak`).
    static func inputAction(id: String, args: [String: Any] = [:]) async throws -> OrchestrateResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "input"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "action": ["id": id, "args": args, "source": "tap"],
        ])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(OrchestrateResponse.self, from: data)
    }

    static func editNote(id: String, text: String) async throws -> EditNoteResponse {
        try await post("notes/edit", ["id": id, "text": text])
    }

    /// One import route for every origin: a PDF or .txt file, page photos
    /// (several = pages), or pasted text. originalId names the device copy.
    static func importProtocol(files: [(name: String, data: Data)], text: String = "",
                               filename: String = "", originalId: String) async throws -> ImportResponse {
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        for f in files {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"\(f.name)\"\r\n".utf8))
            body.append(Data("Content-Type: application/octet-stream\r\n\r\n".utf8))
            body.append(f.data)
            body.append(Data("\r\n".utf8))
        }
        for (k, v) in [("text", text), ("filename", filename), ("original_id", originalId)] where !v.isEmpty {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(k)\"\r\n\r\n".utf8))
            body.append(Data(v.utf8))
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: Config.baseURL.appending(path: "protocols/import"))
        req.httpMethod = "POST"
        req.timeoutInterval = 180  // photos: one transcription per page, then extraction
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (respData, resp) = try await URLSession.shared.data(for: req)
        try check(resp, respData)
        return try JSONDecoder().decode(ImportResponse.self, from: respData)
    }

    /// The photo for a photo question (server POST /look): SAM finds the thing,
    /// Spark reads the crop. ok false: not found or unreadable; the camera stays.
    static func look(searchId: String, attempt: Int, jpeg: Data) async throws -> LookResponse {
        let boundary = "----pip\(UUID().uuidString)"
        var body = Data()
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"look.jpg\"\r\n".utf8))
        body.append(Data("Content-Type: image/jpeg\r\n\r\n".utf8))
        body.append(jpeg)
        body.append(Data("\r\n".utf8))
        for (k, v) in [("search_id", searchId), ("session", "bench"), ("attempt", String(attempt))] {
            body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(k)\"\r\n\r\n".utf8))
            body.append(Data(v.utf8))
            body.append(Data("\r\n".utf8))
        }
        body.append(Data("--\(boundary)--\r\n".utf8))
        var req = URLRequest(url: Config.baseURL.appending(path: "look"))
        req.httpMethod = "POST"
        req.timeoutInterval = 90  // SAM, then reading, maybe a search
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (respData, resp) = try await URLSession.shared.data(for: req)
        try check(resp, respData)
        return try JSONDecoder().decode(LookResponse.self, from: respData)
    }

    /// A photo question's kept crop (the ⑧ thumbnail).
    static func lookCropURL(_ searchId: String) -> URL {
        Config.baseURL.appending(path: "looks/\(searchId)/crop")
    }

    static func protocols() async throws -> [ProtocolSummary] {
        let r: ProtocolListResponse = try await get("protocols")
        return r.protocols
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

    /// What a list screen shows now, so "number 2" and a name pick from it.
    static func reportItems(screen: String, items: [(id: String, name: String)]) async throws {
        let _: [String: JSON] = try await post("screen/items", [
            "screen": screen, "items": items.map { ["id": $0.id, "name": $0.name] }])
    }

    static func searches() async throws -> SearchListResponse {
        try await get("searches")
    }

    static func runDetail(id: String) async throws -> RunDetailResponse {
        try await get("runs/\(id)")
    }

    static func health() async throws -> HealthInfo {
        try await get("health")
    }

    static func record() async throws -> RecordSnapshot {
        let (data, resp) = try await URLSession.shared.data(from: Config.baseURL.appending(path: "record"))
        try check(resp, data)
        return try JSONDecoder().decode(RecordSnapshot.self, from: data)
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
                    case "stage":
                        if let st = try? JSONDecoder().decode(TurnStage.self, from: payload) {
                            cont.yield(.stage(st))
                        }
                    default: break
                    }
                }
                cont.finish()
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }

    /// A server file (a hand off's protocol.py / plate_map.csv) saved as `name` in `dir`.
    static func download(_ path: String, name: String, into dir: URL) async throws -> URL {
        let (data, resp) = try await URLSession.shared.data(from: Config.baseURL.appending(path: path))
        try check(resp, data)
        let url = dir.appending(path: name)
        try data.write(to: url, options: .atomic)
        return url
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
