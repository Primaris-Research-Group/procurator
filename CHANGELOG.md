# Changelog

All notable changes to PROCURATOR. The project was built as one owner's assistant from October 2026 and opened as a public tree at 0.30.0; earlier entries are condensed from the internal release notes.

## 0.30.0 — 2026-11-08 — first public source

- **Editions.** PROCURATOR (standard) and PROCURATOR-X (adds the coding orchestrator) from one tree: `"edition": "x"` on connector manifests, `install.sh --edition`, `PROCURATOR_EDITION` honoured by the brokers and the plugin gate; x-only tools refuse on a standard box and are absent from its capability card.
- **De-personalized runtime.** Owner id from `PROCURATOR_OWNER_ID` / `config/owner.json` (no literals), mail accounts from the Accounts store, public base URL from `PROCURATOR_PUBLIC_BASE`, host from `PROCURATOR_HOST`; service, socket and state names `procurator-*`; install dir `/opt/procurator`.
- Repository files: LICENSE (Apache-2.0), NOTICE, SECURITY, CONTRIBUTING, CODE_OF_CONDUCT, RELEASE.json, config examples, CI (tests, derived files, hygiene), website (`site/`).
- **Lay-person install (Tier 1).** The installer adds HTTPS (Caddy; a free `<ip>.sslip.io` name when there is no domain) and prints a one-time **browser setup link**: model key, Telegram bot, and tap-to-pair (`t.me/<bot>?start=<one-time code>`; whoever presses Start becomes the owner — `config/owner.json`, `PROCURATOR_OWNER_ID` and a Telegram allow-list are written, no pairing code). Privileged steps run in a root one-shot unit (`procurator-setup.path`) so the web broker stays sandboxed. `connect-accounts.py` does the same in the terminal. New **Setup** tab; numbered app-password walk-throughs and clearer errors on **Accounts**; Apple app passwords accepted with or without dashes.
- **Welcome interview.** After setup, the bot asks what to call the owner, their pronouns (he/him, she/her, they/them), city, work, what they want help with and their main email; every turn then carries the owner's name and pronouns. Built-in prompts now say "the owner"/"their". `/welcome` redoes it.
- **Gateway configuration on fresh installs** (`bin/configure-gateway.py`): plugins loaded with hooks allowed, every declared tool allow-listed (experimental modules off), agent defaults, DuckDuckGo search, daily reset, unused bundled skills off. Telegram setup also sets owner-only commands, no link previews and table-friendly formatting.
- Export tooling: `bin/export_release.py` (allow-list, renames, reviewed rewrites, overlay, denylist gate).

## 0.29.0 — 2026-11-08 — Apple and Google accounts
- Dashboard **Accounts** tab: one email + app password per account, verified live (IMAP, SMTP, CalDAV, CardDAV); secrets 0600; service chips.
- `icloud_dav.py` (stdlib CalDAV/CardDAV); tools `accounts_list`, `calendar_apple_events`, `contacts_apple_search`, `calendar_apple_create_event`.
- Google OAuth provider with sensitive scopes only; `bin/set-oauth-client.sh`; `bin/install.sh` box bootstrap.

## 0.28.0 — contracts library live · 0.27.0 — public posts and contracts · 0.26.0 — wellness coach · 0.25.0 — connector gate and health module
- Deny-by-default connector manifests (risk classes, policy file, plugin gate), hard-block policy file, health grants and taint checks, coach and nutrition estimates (pending clinician review), public posting with audiences, contracts library.

## 0.24.x — Packet Composer · 0.23.0 — health module · 0.22.0 — health portal and mock EHR
- Composer: guided chat that writes task packets; per-step approvals; Composer bench.
- Health logistics: visits, refills, portal paperwork under explicit grants; synthetic EHR and pharmacy for tests.

## 0.20.x – 0.21.x — browsing beyond the plan
- Task-level approvals for web errands (`/webtask`), no-double-submit guard, live frames and takeover, compat battery, home-PC browser lane, whole-document reading, support and memory tools.

## 0.16 – 0.19 — browsing build plan phases
- Cloud browser lane, site rules, saved logins the owner performs, form filling, booking limits, page-vision checks, bench and regression suites.

## 0.9 – 0.15 — foundations
- Telegram owner channel, mail broker (Gmail, iCloud), voice (Twilio + realtime), task packets (missions), dashboard and orchestrator, approvals bound to content hashes, activity ledger, usage meter and model switching.
