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

struct OrchestrateResponse: Decodable {
    let tool_calls: [ToolCall]
    let reply: String
    let ms: [String: Int]
    let route: String?
}

struct VoiceResponse: Decodable {
    let transcript: String
    let tool_calls: [ToolCall]
    let reply: String
    let ms: [String: Int]
    let route: String?
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
    static func orchestrate(text: String, state: [String: Any]) async throws -> OrchestrateResponse {
        var req = URLRequest(url: Config.baseURL.appending(path: "orchestrate"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text, "state": state])
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(OrchestrateResponse.self, from: data)
    }

    static func voice(wav: Data, state: [String: Any]) async throws -> VoiceResponse {
        let boundary = "----pip\(UUID().uuidString)"
        let stateData = try JSONSerialization.data(withJSONObject: state)
        var body = Data()
        func append(_ s: String) { body.append(Data(s.utf8)) }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"audio\"; filename=\"clip.wav\"\r\n")
        append("Content-Type: audio/wav\r\n\r\n")
        body.append(wav)
        append("\r\n--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"state\"\r\n\r\n")
        body.append(stateData)
        append("\r\n--\(boundary)--\r\n")

        var req = URLRequest(url: Config.baseURL.appending(path: "voice"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await URLSession.shared.data(for: req)
        try check(resp, data)
        return try JSONDecoder().decode(VoiceResponse.self, from: data)
    }

    private static func check(_ resp: URLResponse, _ data: Data) throws {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            throw PipError.server(code, String(data: data, encoding: .utf8) ?? "")
        }
    }
}
