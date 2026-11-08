import Foundation

/// Talks to ONE PROCURATOR installation. https only; the system's certificate validation is never relaxed (no custom trust handler),
/// so a certificate failure stops the request. Redirects are refused, so a batch can never be sent to another host.
final class APIClient: NSObject, URLSessionTaskDelegate {
    enum Failure: Error, LocalizedError {
        case insecureURL, badResponse(Int), server(ServerError), offline(Error)
        var errorDescription: String? {
            switch self {
            case .insecureURL: return "Use the https:// address PROCURATOR gave you."
            case .badResponse(let s): return "The server answered HTTP \(s)."
            case .server(let e): return e.detail ?? e.error
            case .offline(let e): return "Could not reach the server (\(e.localizedDescription)). Nothing was lost; try again."
            }
        }
        var isAuth: Bool { if case .server(let e) = self { return ["revoked", "expired", "unknown_device", "no_credential"].contains(e.error) }; return false }
    }

    let base: URL
    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 30
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()

    /// `server` is the dashboard address, e.g. https://assistant.example.com/assistant/dashboard
    init(server: String) throws {
        guard let u = URL(string: server.trimmingCharacters(in: .whitespacesAndNewlines)), u.scheme == "https", let host = u.host, !host.isEmpty, u.user == nil else {
            throw Failure.insecureURL
        }
        base = u
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest) async -> URLRequest? {
        nil
    }

    private func post<T: Decodable>(_ path: String, body: Data, credential: String? = nil) async throws -> T {
        var req = URLRequest(url: base.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let c = credential { req.setValue("Bearer " + c, forHTTPHeaderField: "Authorization") }
        req.httpBody = body
        let data: Data, resp: URLResponse
        do { (data, resp) = try await session.data(for: req) } catch { throw Failure.offline(error) }
        guard let http = resp as? HTTPURLResponse else { throw Failure.badResponse(0) }
        if (200..<300).contains(http.statusCode) { return try JSONDecoder().decode(T.self, from: data) }
        if let e = try? JSONDecoder().decode(ServerError.self, from: data) { throw Failure.server(e) }
        throw Failure.badResponse(http.statusCode)
    }

    func pair(code: String, deviceName: String) async throws -> PairResponse {
        try await post("health/pair", body: JSONSerialization.data(withJSONObject: ["code": code, "device_name": deviceName]))
    }

    func sync(_ envelope: Envelope, credential: String) async throws -> Ack {
        try await post("health/sync", body: JSONEncoder().encode(envelope), credential: credential)
    }
}
