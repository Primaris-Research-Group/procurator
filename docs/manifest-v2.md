# Connector manifest v2 (0.28.0)

One JSON file per connector (`integrations/web/connectors/*.json`; Phase 3a moves them into modules). `manifest_version: 2`
requires every field below that applies; version 1 files (packs written before 0.27.0) are read with defaults: an `external` tool
is `audience: person`, an outbound `own_write` tool is `audience: self`.

## Connector
`id, version, title, description, class (general|health), source (core|pack), plugin, auth {type, scopes}, tools, actions {web|mail: [...]},
accounts (single|multiple), rate_limit {per_hour, per_day, cooldown_s}, events [...], inbound {types, signature (slack-v0|procurator-hmac|teams-jwt|meta-sha256), headers},
min_data + fixtures (health), sha256 (pack), setup, notes`

## Tool
| Field | Values | Rule |
| --- | --- | --- |
| `risk` | read, draft, own_write, external, public, owner | packs cannot declare owner tools |
| `outbound` | bool | text may leave: checked against labels before it does |
| `audience` | public, workspace, person, self | required on outbound, external and public tools; external = person; public = public or workspace; health connectors never reach public or workspace |
| `destination` | argument name | the recipient (email, phone, URL, browser session) |
| `account` | argument name | required on outbound tools of a multi-account connector; bound into the approval hash |
| `media` | argument name | media refs; each file's sha256 is bound into the approval hash |
| `rate_limit` | {per_hour, per_day, cooldown_s} | per account, enforced by the broker before the provider call |
| `effect` | data (default), physical | |
| `safety_tier` | comfort, access, safety | required when physical |
| `confirm` | readback | required when physical: results are confirmed, unconfirmed or failed |
| `bounds` | {param: {min, max, enum, window "HH:MM-HH:MM"}} | limits for bounded standing grants |

## Policies the broker applies from these fields
- Labels (`health`, `presence`, `camera`) never reach a public or workspace audience; presence and camera only reach the owner.
- Public and workspace tools get the publish preview (account, visibility, media hashes, exact text) and `/postsend`.
- Physical: comfort inside a `/homegrant` runs; access always `/homeapprove` and never from packets, schedules, Composer, MCP requests or events; safety disable/mute/remove are hard blocks (`policy/hard-blocks.json`); `/homeoff` refuses all.
- Mail: owner copy per audience (`person` → Bcc by default, `/mailautocc`).
