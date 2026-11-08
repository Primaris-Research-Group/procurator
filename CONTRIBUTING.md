# Contributing

Thank you for helping. PROCURATOR is small enough to read and strict enough to break if you skip the rules below.

## Ground rules

1. **No owner literals.** Nothing in the tree names a person, a chat id, a host, a phone number or an address. Use `owner@example.com`, `+1 555 010 0100`, `203.0.113.10`, `assistant.example.com`. CI's hygiene job fails otherwise.
2. **Every tool is declared.** A new model tool needs an entry in a connector manifest (`integrations/web/connectors/*.json`) with a risk class, then `python3 integrations/web/connectors.py policy integrations/web-plugin/connector-policy.json` (copied to the mail and voice plugins). Undeclared tools refuse at run time and fail the inventory test.
3. **Every external action is approved.** If a tool sends, posts, calls, submits or pays, it is `external` and its text is bound in an approval hash. Do not add a second approval path.
4. **Standard library only** in the Python brokers; the plugins have no npm dependencies (OpenClaw provides the runtime).
5. **Tests go with the change.** Python: `PROCURATOR_NO_KEYS=1 python3 -m unittest` in each `integrations/*` directory and in `tests/`. Plugins: `node --test` in `integrations/*-plugin`. The 13 `runner.*.test.cjs` need `playwright-core` + Chromium.
6. **Editions.** A feature that belongs to PROCURATOR-X carries `"edition": "x"` in its manifest and must be inert on a standard box (see `docs/editions.md`).
7. **Untrusted text stays untrusted.** Anything read from a page, a message, a document or a transcript is fenced and never used as an instruction.

## Workflow

- Fork, branch from `main`, keep pull requests focused.
- Run the suites locally; CI runs them again plus the hygiene and inventory checks.
- Sign off your commits (`git commit -s`, Developer Certificate of Origin).
- For anything touching approvals, hard blocks or the health gate, open an issue first.

## Releases

Versions live in `RELEASE.json`; `bin/release.py check` (CI) verifies the plugin manifests agree with it. Tag `vX.Y.Z` on `main` to publish a release.
