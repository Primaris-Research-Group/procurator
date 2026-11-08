import Foundation
import Security

/// The device credential lives only in the Keychain, readable after first unlock, never synced to other devices.
enum Keychain {
    private static let service = "org.primaris.procurator.health"

    static func save(_ secret: String, account: String) -> Bool {
        delete(account: account)
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account,
                                kSecValueData as String: Data(secret.utf8), kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess
    }

    static func load(account: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account,
                                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    @discardableResult
    static func delete(account: String) -> Bool {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        return SecItemDelete(q as CFDictionary) == errSecSuccess
    }
}

/// Non-secret state in Application Support: the pairing, per-type anchors (saved only after an ack) and the one pending batch.
final class LocalState {
    private let dir: URL
    init(directory: URL? = nil) {
        dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("ProcuratorHealth", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }
    private func url(_ name: String) -> URL { dir.appendingPathComponent(name) }

    private func read<T: Decodable>(_ name: String, _ t: T.Type) -> T? {
        guard let d = try? Data(contentsOf: url(name)) else { return nil }
        return try? JSONDecoder().decode(T.self, from: d)
    }
    private func write<T: Encodable>(_ name: String, _ v: T?) {
        if let v = v, let d = try? JSONEncoder().encode(v) {
            try? d.write(to: url(name), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } else {
            try? FileManager.default.removeItem(at: url(name))
        }
    }

    var pairing: Pairing? { get { read("pairing.json", Pairing.self) } set { write("pairing.json", newValue) } }
    var anchors: [String: Data] { get { read("anchors.json", [String: Data].self) ?? [:] } set { write("anchors.json", newValue) } }
    /// A batch built but not yet acknowledged, with the anchors it would commit. Re-sent with the SAME batch id after a crash/offline.
    var pending: PendingBatch? { get { read("pending.json", PendingBatch.self) } set { write("pending.json", newValue) } }

    func reset() { for n in ["pairing.json", "anchors.json", "pending.json"] { try? FileManager.default.removeItem(at: url(n)) } }
}

struct PendingBatch: Codable {
    var envelope: Envelope
    var anchors: [String: Data]
}
