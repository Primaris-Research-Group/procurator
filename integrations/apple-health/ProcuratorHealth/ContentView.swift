import SwiftUI

@main
struct ProcuratorHealthApp: App {
    @StateObject private var engine = SyncEngine(source: HKHealthSource.available ? HKHealthSource() : FakeHealthSource())
    var body: some Scene {
        WindowGroup { ContentView().environmentObject(engine) }
    }
}

struct ContentView: View {
    @EnvironmentObject var engine: SyncEngine
    @State private var server = "https://"
    @State private var code = ""
    @State private var confirmDisconnect = false

    var body: some View {
        NavigationStack {
            Form {
                if let p = engine.pairing {
                    Section("Connected") {
                        LabeledContent("Installation", value: p.installation)
                        LabeledContent("Server", value: p.server.host ?? "")
                        LabeledContent("Last sync", value: p.lastSync.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? "never")
                        if let last = p.lastSync, Date().timeIntervalSince(last) > 2 * 86400 { Text("Stale: not synced for over two days.").foregroundStyle(.orange) }
                        if engine.hasPending { Text("One batch is waiting to be sent.").foregroundStyle(.secondary) }
                    }
                    Section("Share with PROCURATOR") {
                        ForEach(ShareType.allCases) { t in
                            Toggle(t.label, isOn: Binding(get: { p.enabled.contains(t) }, set: { on in Task { await engine.setEnabled(t, on) } }))
                                .disabled(!p.types.contains(t))
                        }
                        Text("Daily steps and energy are sent as daily totals for the last 14 days.").font(.footnote).foregroundStyle(.secondary)
                    }
                    Section {
                        Button("Preview what will be sent") { Task { await engine.makePreview() } }
                        Button("Sync now") { Task { await engine.syncNow() } }.disabled(engine.busy || p.enabled.isEmpty)
                        if engine.noReadableData {
                            Text("No readable data. Health does not tell apps whether reading was denied; check Settings › Health › Data Access & Devices.")
                                .font(.footnote)
                        }
                    }
                    if let pv = engine.preview {
                        Section("Preview (nothing sent yet)") { ScrollView(.horizontal) { Text(pv).font(.system(.caption2, design: .monospaced)).textSelection(.enabled) } }
                    }
                    Section { Button("Disconnect this phone", role: .destructive) { confirmDisconnect = true } }
                } else {
                    Section("Pair with your PROCURATOR") {
                        TextField("Server address (https://…/assistant/dashboard)", text: $server).textInputAutocapitalization(.never).keyboardType(.URL).autocorrectionDisabled()
                        TextField("Code from /healthpair", text: $code).textInputAutocapitalization(.characters).autocorrectionDisabled()
                        Button("Pair") { Task { await engine.pair(server: server, code: code, deviceName: UIDevice.current.name) } }
                            .disabled(engine.busy || code.count < 8 || !server.hasPrefix("https://"))
                    }
                }
                if !engine.status.isEmpty { Section { Text(engine.status) } }
            }
            .navigationTitle("PROCURATOR Health")
            .confirmationDialog("Disconnect this phone? It stops sending. The server keeps what it has until you remove it with /healthdevices.", isPresented: $confirmDisconnect, titleVisibility: .visible) {
                Button("Disconnect", role: .destructive) { engine.disconnect() }
            }
        }
    }
}

#Preview {
    ContentView().environmentObject(SyncEngine(source: FakeHealthSource(), state: LocalState(directory: FileManager.default.temporaryDirectory)))
}
