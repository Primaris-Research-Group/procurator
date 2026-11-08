# PROCURATOR Health — iPhone companion (source, v0.1, unbuilt)

Build plan: `build-plans/health/05-apple-health.md`. Server side: `integrations/web/health_sync.py` (web 0.23.0).

This folder holds the SwiftUI sources. It has **not been compiled or run on a device yet**; the first build happens on the Mac in
Xcode. HealthKit only works on a physical iPhone (the simulator has no real Health data), so device acceptance is a separate step.

## What it does (milestones 1–3)
- Pairs with one PROCURATOR installation using the single-use code from `/healthpair` (Telegram). Only `https://` server addresses are
  accepted; the app shows the installation name and the data types the server will accept before anything is sent.
- Stores the per-device credential in the iOS Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`).
- Reads only the types the owner switches on: body mass, height (samples, via anchored queries incl. deletions), daily steps and active
  energy (via `HKStatisticsCollectionQuery`, which already merges iPhone + Apple Watch — raw samples are never summed).
- Manual "Sync now". Each batch has a stable id and a per-device sequence number; HealthKit anchors are saved **only after** the server
  acknowledges the batch, and an unsent batch is kept on disk and re-sent with the same id (the server answers duplicates idempotently).
- "Preview" shows the exact JSON that would be sent.
- Disconnect deletes the credential and local queue; `/healthdevices revoke ID delete` removes the server copy.
- Honest permission state: HealthKit does not reveal whether *read* access was denied, so an empty result is shown as
  "No readable data — check Settings › Health › Data Access & Devices › PROCURATOR Health", never as "denied" or "granted".

Not yet: background delivery, nutrition write-back, clinical records.

## Create the Xcode project (Mac, Xcode 16+)
1. File › New › Project › iOS App. Product name `PROCURATOR Health`, interface SwiftUI, language Swift, minimum iOS 17.0.
2. Delete the generated `ContentView.swift` and `…App.swift`; drag in every `.swift` file from `ProcuratorHealth/` (Copy items if needed).
3. Target › Signing & Capabilities › `+ Capability` › **HealthKit** (leave Clinical Health Records and Background Delivery off).
   Use `ProcuratorHealth/ProcuratorHealth.entitlements` if Xcode does not create one.
4. Target › Info: add the two keys from `ProcuratorHealth/Info-additions.plist`
   (`NSHealthShareUsageDescription`, `NSHealthUpdateUsageDescription`).
5. Run on the owner's iPhone (a free Apple ID works for personal device installs; TestFlight needs the paid program).
6. On the phone: in Telegram send `/healthpair`, open the app, enter `https://assistant.example.com/assistant/dashboard` and the code.

## Device acceptance checklist (record results in the qualification report)
Partial authorization, empty store, denied reads, revoked device (`/healthdevices revoke`), locked phone, offline then online,
expired credential, server certificate failure (try a wrong host), duplicate batch (kill the app mid-sync), deleted weight sample,
reinstall, a second device, a daylight-saving day, travel across time zones.
