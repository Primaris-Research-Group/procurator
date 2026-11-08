import Foundation

/// The data types the owner can share. Raw values match the server (health_sync.py TYPES).
enum ShareType: String, Codable, CaseIterable, Identifiable {
    case bodyMass = "body_mass", height = "height", stepCount = "step_count", activeEnergy = "active_energy",
         basalEnergy = "basal_energy", exerciseMinutes = "exercise_minutes"   // 0.26.0: for the wellness coach
    var id: String { rawValue }
    var label: String {
        switch self {
        case .bodyMass: return "Weight"
        case .height: return "Height"
        case .stepCount: return "Daily steps"
        case .activeEnergy: return "Active energy (daily)"
        case .basalEnergy: return "Resting energy (daily)"
        case .exerciseMinutes: return "Exercise minutes (daily)"
        }
    }
    var isStatistic: Bool { self == .stepCount || self == .activeEnergy || self == .basalEnergy || self == .exerciseMinutes }
}

/// Envelope version 1 (see health_sync.py docstring).
struct Envelope: Codable, Equatable {
    var version = 1
    var batch_id: String
    var sequence: Int
    var samples: [Sample]
    var deletions: [Deletion]
    var statistics: [Statistic]
    var isEmpty: Bool { samples.isEmpty && deletions.isEmpty && statistics.isEmpty }
}

struct Sample: Codable, Equatable {
    var type: String
    var source_id: String       // HealthKit UUID: stable across reinstall and devices
    var start: String
    var end: String
    var tz: String
    var value: Double
    var unit: String
    var source_app: String?
    var source_device: String?
}

struct Deletion: Codable, Equatable {
    var type: String
    var source_id: String
}

struct Statistic: Codable, Equatable {
    var type: String
    var date: String            // local calendar day, YYYY-MM-DD
    var tz: String
    var value: Double
    var unit: String
    var method = "HKStatisticsCollectionQuery cumulativeSum"
}

struct PairResponse: Codable {
    var device_id: String
    var credential: String
    var installation: String
    var types: [String]
    var envelope_version: Int
    var credential_expires_days: Int
}

struct Ack: Codable {
    var batch_id: String
    var accepted: Int
    var deleted: Int
    var statistics: Int
    var ack: Bool?
    var duplicate: Bool?
}

struct ServerError: Codable, Error {
    var error: String
    var detail: String?
}

/// What the app remembers between launches (not secret; the credential is in the Keychain).
struct Pairing: Codable, Equatable {
    var server: URL
    var installation: String
    var deviceID: String
    var types: [ShareType]
    var enabled: [ShareType]
    var lastSync: Date?
    var lastSequence: Int = 0
}

enum Iso {
    static func string(_ d: Date, tz: TimeZone) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = tz
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }
    static func day(_ d: Date, tz: TimeZone) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = tz
        let c = cal.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
    }
}
