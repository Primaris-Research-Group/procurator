import Foundation
import SwiftUI

/// Pairing, preview and manual sync. Invariants:
///  * nothing is read or sent for a type the owner has not switched on;
///  * a batch keeps its id until the server acknowledges it; only then are its HealthKit anchors saved;
///  * an auth failure (revoked / expired) stops syncing and asks to pair again; the pending batch is dropped, not sent elsewhere.
@MainActor
final class SyncEngine: ObservableObject {
    @Published var pairing: Pairing?
    @Published var status = ""
    @Published var busy = false
    @Published var preview: String?
    @Published var noReadableData = false

    private let source: HealthSource
    private let state: LocalState
    private let statisticDays = 14

    init(source: HealthSource, state: LocalState = LocalState()) {
        self.source = source
        self.state = state
        self.pairing = state.pairing
        if let p = pairing, Keychain.load(account: p.deviceID) == nil {      // reinstall: Keychain empty → pair again
            state.reset(); pairing = nil
        }
    }

    var hasPending: Bool { state.pending != nil }

    func pair(server: String, code: String, deviceName: String) async {
        busy = true; defer { busy = false }
        do {
            let api = try APIClient(server: server)
            let r = try await api.pair(code: code.uppercased(), deviceName: deviceName)
            guard r.envelope_version == 1, Keychain.save(r.credential, account: r.device_id) else { status = "Pairing failed (could not store the credential)."; return }
            let types = r.types.compactMap(ShareType.init(rawValue:))
            state.reset()
            let p = Pairing(server: api.base, installation: r.installation, deviceID: r.device_id, types: types, enabled: [])
            state.pairing = p; pairing = p
            status = "Paired with \(r.installation) at \(api.base.host ?? ""). Choose what to share, then tap Sync."
        } catch {
            status = (error as? LocalizedError)?.errorDescription ?? "Pairing failed."
        }
    }

    func setEnabled(_ t: ShareType, _ on: Bool) async {
        guard var p = pairing, p.types.contains(t) else { return }
        if on {
            do { try await source.requestAuthorization([t]) } catch { status = "Health is not available on this device."; return }
            if !p.enabled.contains(t) { p.enabled.append(t) }
        } else {
            p.enabled.removeAll { $0 == t }
        }
        state.pairing = p; pairing = p
    }

    /// Build the next batch (or reuse the pending one). Does not change any saved anchor.
    private func nextBatch() async throws -> PendingBatch? {
        if let pend = state.pending { return pend }
        guard let p = pairing else { return nil }
        var anchors = state.anchors
        var samples: [Sample] = [], deletions: [Deletion] = [], stats: [Statistic] = []
        for t in p.enabled {
            if t.isStatistic {
                stats += try await source.dailyTotals(t, days: statisticDays)
            } else {
                let r = try await source.changes(t, since: anchors[t.rawValue])
                samples += r.samples; deletions += r.deletions
                if let a = r.anchor { anchors[t.rawValue] = a }
            }
        }
        let env = Envelope(batch_id: "\(p.deviceID)-\(p.lastSequence + 1)-\(UUID().uuidString.prefix(8))", sequence: p.lastSequence + 1,
                           samples: samples, deletions: deletions, statistics: stats)
        let pend = PendingBatch(envelope: env, anchors: anchors)
        state.pending = pend           // persisted BEFORE sending: a crash re-sends this exact batch
        return pend
    }

    func makePreview() async {
        do {
            guard let b = try await nextBatch() else { return }
            let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
            preview = String(decoding: try enc.encode(b.envelope), as: UTF8.self)
            noReadableData = b.envelope.isEmpty && state.anchors.isEmpty
        } catch { status = "Could not read Health data." }
    }

    func syncNow() async {
        guard let p = pairing, let cred = Keychain.load(account: p.deviceID) else { status = "Not paired."; return }
        busy = true; defer { busy = false }
        do {
            guard let batch = try await nextBatch() else { return }
            if batch.envelope.isEmpty && state.anchors.isEmpty {
                noReadableData = true
                status = "No readable data. If you expected some, check Settings › Health › Data Access & Devices › PROCURATOR Health."
            }
            let api = try APIClient(server: p.server.absoluteString)
            let ack = try await api.sync(batch.envelope, credential: cred)
            guard ack.batch_id == batch.envelope.batch_id else { status = "The server acknowledged a different batch; nothing was marked as sent."; return }
            state.anchors = batch.anchors                // only now
            state.pending = nil
            var np = p; np.lastSync = Date(); np.lastSequence = batch.envelope.sequence
            state.pairing = np; pairing = np
            status = "Synced: \(ack.accepted) measurements, \(ack.deleted) deletions, \(ack.statistics) daily totals."
            preview = nil
        } catch let f as APIClient.Failure where f.isAuth {
            state.pending = nil
            status = "This phone was disconnected or its pairing expired. Pair again with /healthpair."
        } catch {
            status = ((error as? LocalizedError)?.errorDescription ?? "Sync failed.") + " The batch is kept and will be re-sent unchanged."
        }
    }

    func disconnect() {
        if let p = pairing { Keychain.delete(account: p.deviceID) }
        state.reset(); pairing = nil; preview = nil
        status = "Disconnected on this phone. To delete what the server has, send /healthdevices revoke ID delete."
    }
}
