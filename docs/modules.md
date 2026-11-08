# Modules

Modules are groups of connectors. Core modules are always on; optional ones are enabled on the dashboard's Modules page (in development) or with `PROCURATOR_TEST_PROFILE` for the synthetic test services.

| Module | Connectors | Default |
|---|---|---|
| Core | assistant-core, activity-log, memory, library, documents, media, research, places, npi-registry, platform, platform-stream, inbound-events, connections, core-accounts | on |
| Mail | gmail, icloud-mail | on (after accounts are connected) |
| Google PIM | google-calendar, google-contacts, google-tasks, google-read-mcp, google-output-mcp | on (after Google sign-in) |
| Browser | browser | on (cloud lane needs a Browserbase key; home lane needs the worker) |
| Phone and SMS | phone, sms | on (after Twilio) |
| Missions | missions, mcp-server | on |
| Builds | builds | **PROCURATOR-X only** |
| Health (experimental) | health-core, health-portal, health-apple, health-pharmacy, health-coach, health-nutrition | off |
| Social and home (experimental) | social, home, finance-readonly | off |
