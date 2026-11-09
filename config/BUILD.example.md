---
repo: your-org/your-repo
base: main
agent: claude
model: opus
builder: any
paths: [src/, tests/]
max_minutes: 45
---
# Add a /healthz endpoint

## Goal
Add a GET /healthz route to the web service in src/server.py that returns {"ok": true} with status 200.
Register it next to the existing routes and keep the style of the file.

## Acceptance
- [ ] GET /healthz returns 200 and {"ok": true}
- [ ] a unit test in tests/ covers it
- [ ] existing tests still pass

## Notes
Do not add dependencies. If the service already has a health route under another name, ask before adding a second one.
