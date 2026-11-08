import Foundation
import HealthKit

/// What the sync engine needs from HealthKit. `HKHealthSource` is the real one; `FakeHealthSource` drives previews and tests.
protocol HealthSource {
    func requestAuthorization(_ types: [ShareType]) async throws
    /// New/changed samples and deletions since the stored anchor. The new anchor is returned, NOT saved: the engine saves it after the ack.
    func changes(_ type: ShareType, since anchor: Data?) async throws -> (samples: [Sample], deletions: [Deletion], anchor: Data?)
    /// Daily totals (statistics merge phone + watch, so overlapping samples are not double counted).
    func dailyTotals(_ type: ShareType, days: Int) async throws -> [Statistic]
}

enum HealthSourceError: Error { case unavailable }

final class HKHealthSource: HealthSource {
    private let store = HKHealthStore()

    static var available: Bool { HKHealthStore.isHealthDataAvailable() }

    private func hkType(_ t: ShareType) -> HKQuantityType {
        switch t {
        case .bodyMass: return HKQuantityType(.bodyMass)
        case .height: return HKQuantityType(.height)
        case .stepCount: return HKQuantityType(.stepCount)
        case .activeEnergy: return HKQuantityType(.activeEnergyBurned)
        case .basalEnergy: return HKQuantityType(.basalEnergyBurned)
        case .exerciseMinutes: return HKQuantityType(.appleExerciseTime)
        }
    }

    private func unit(_ t: ShareType) -> (HKUnit, String) {
        switch t {
        case .bodyMass: return (.gramUnit(with: .kilo), "kg")
        case .height: return (.meterUnit(with: .centi), "cm")
        case .stepCount: return (.count(), "count")
        case .activeEnergy: return (.kilocalorie(), "kcal")
        case .basalEnergy: return (.kilocalorie(), "kcal")
        case .exerciseMinutes: return (.minute(), "min")
        }
    }

    func requestAuthorization(_ types: [ShareType]) async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthSourceError.unavailable }
        // Read access only. HealthKit never tells an app whether READ access was denied; an empty answer may mean either.
        try await store.requestAuthorization(toShare: [], read: Set(types.map { hkType($0) as HKObjectType }))
    }

    func changes(_ type: ShareType, since anchor: Data?) async throws -> (samples: [Sample], deletions: [Deletion], anchor: Data?) {
        let hk = hkType(type)
        let (u, unitName) = unit(type)
        let start = anchor.flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
        let descriptor = HKAnchoredObjectQueryDescriptor(predicates: [.quantitySample(type: hk)], anchor: start)
        let result = try await descriptor.result(for: store)
        let tz = TimeZone.current
        let samples = result.addedSamples.map { s -> Sample in
            let sampleTZ = (s.metadata?[HKMetadataKeyTimeZone] as? String).flatMap(TimeZone.init(identifier:)) ?? tz
            return Sample(type: type.rawValue, source_id: s.uuid.uuidString, start: Iso.string(s.startDate, tz: sampleTZ), end: Iso.string(s.endDate, tz: sampleTZ),
                          tz: sampleTZ.identifier, value: s.quantity.doubleValue(for: u), unit: unitName,
                          source_app: s.sourceRevision.source.name, source_device: s.device?.name)
        }
        let deletions = result.deletedObjects.map { Deletion(type: type.rawValue, source_id: $0.uuid.uuidString) }
        let newAnchor = try NSKeyedArchiver.archivedData(withRootObject: result.newAnchor, requiringSecureCoding: true)
        return (samples, deletions, newAnchor)
    }

    func dailyTotals(_ type: ShareType, days: Int) async throws -> [Statistic] {
        let hk = hkType(type)
        let (u, unitName) = unit(type)
        let tz = TimeZone.current
        var cal = Calendar.current
        cal.timeZone = tz
        let end = Date()
        let startDay = cal.startOfDay(for: cal.date(byAdding: .day, value: -(days - 1), to: end)!)
        let predicate = HKSamplePredicate.quantitySample(type: hk, predicate: HKQuery.predicateForSamples(withStart: startDay, end: end))
        let query = HKStatisticsCollectionQueryDescriptor(predicate: predicate, options: .cumulativeSum, anchorDate: startDay, intervalComponents: DateComponents(day: 1))
        let collection = try await query.result(for: store)
        var out: [Statistic] = []
        collection.enumerateStatistics(from: startDay, to: end) { stats, _ in
            if let q = stats.sumQuantity() {
                out.append(Statistic(type: type.rawValue, date: Iso.day(stats.startDate, tz: tz), tz: tz.identifier, value: q.doubleValue(for: u).rounded(), unit: unitName))
            }
        }
        return out
    }
}

/// Synthetic data for SwiftUI previews and unit tests (milestone 1: pairing + payload preview without a real Health store).
final class FakeHealthSource: HealthSource {
    var store: [ShareType: [Sample]] = [:]
    var deleted: [ShareType: [Deletion]] = [:]
    var totals: [ShareType: [Statistic]] = [:]

    init() {
        let tz = TimeZone(identifier: "America/New_York")!
        let now = Date()
        store[.bodyMass] = [Sample(type: "body_mass", source_id: "00000000-FAKE-0000-0000-000000000001", start: Iso.string(now, tz: tz), end: Iso.string(now, tz: tz),
                                   tz: tz.identifier, value: 80.4, unit: "kg", source_app: "Health", source_device: "Synthetic scale")]
        totals[.stepCount] = [Statistic(type: "step_count", date: Iso.day(now, tz: tz), tz: tz.identifier, value: 6400, unit: "count")]
    }

    func requestAuthorization(_ types: [ShareType]) async throws {}

    func changes(_ type: ShareType, since anchor: Data?) async throws -> (samples: [Sample], deletions: [Deletion], anchor: Data?) {
        let a = anchor.map { String(decoding: $0, as: UTF8.self) }
        if a == "seen" { return ([], deleted[type] ?? [], Data("seen".utf8)) }
        return (store[type] ?? [], deleted[type] ?? [], Data("seen".utf8))
    }

    func dailyTotals(_ type: ShareType, days: Int) async throws -> [Statistic] { totals[type] ?? [] }
}
