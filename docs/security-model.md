# Security model

## Boundaries

```
Telegram (owner) ──▶ OpenClaw gateway (container, no credentials) ──tools.sock──▶ brokers (root, systemd, hold credentials)
                       ▲ owner commands only                        approval.sock ─┘
                       └── untrusted text: pages, mail bodies, documents, transcripts (fenced)
```

1. **The model never holds a credential.** Keys, app passwords and OAuth tokens live with the brokers under `secrets/` (0600). The gateway container reaches the brokers only through two Unix sockets.
2. **Deny by default.** A tool exists for the model only if a connector manifest declares it (`integrations/web/connectors/*.json`) with a risk class: `read`, `draft`, `own_write`, `owner`, `external`, `public`. The broker refuses undeclared actions; the plugin gate refuses undeclared tools; CI fails when a registered tool is undeclared.
3. **Approvals bound to content.** Every `external`/`public` action produces a review message with the exact content and a hash. The owner replies with a native command (`/mailsend ID CODE`, `/callapprove`, `/webtask`, `/smssend`, `/buildapprove`, …). A changed draft invalidates the code. Approvals arrive on `approval.sock`, which the model cannot reach.
4. **Hard blocks in code.** `integrations/web/policy/hard-blocks.json`: payments above the owner's cap, creating accounts, messages to unknown numbers, deletions, publishing health values — refused regardless of prompt.
5. **Untrusted text is fenced.** Web pages, email, documents, call transcripts and calendar text are wrapped before the model sees them and can never grant a permission; a second-press check journals repeated submissions.
6. **Health values are labeled** at the source and checked at every exit by the taint check; they go only to recipients a health grant names, and never to public or workspace audiences.
7. **Everything is logged** to the activity ledger (calls, browser tasks, emails, calendar changes) and visible in the dashboard and `/meter`.

## Open items (0.30.0)
- H1: a per-boot secret on `approval.sock` (privilege today is by socket path and file mode).
- M1: `/webtask` binds 16 hex characters of the hash; the full hash should be checked server-side.
- Independent audit.
