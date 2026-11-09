# Editions: PROCURATOR and PROCURATOR-X

One codebase, one version number, one switch.

| | PROCURATOR (`standard`) | PROCURATOR-X (`x`) |
|---|---|---|
| Everything in the README feature list | ✓ | ✓ |
| Coding orchestrator: `build_prepare` (BUILD.md, model), `build_status`, `build_cancel`, `/buildapprove`, `/buildnote`, `/buildlog`, Builds tab with live log, builder relay, Composer "coding" packets ([docs/builds.md](builds.md)) | — | ✓ |
| Box size | 4 GB | 8 GB recommended |
| Companion role | home browser lane | + builder (Claude Code / Codex on your Mac or PC) |

## How the switch works

- `bin/install.sh --edition x` writes `EDITION=x` to `/etc/procurator/instance.env` (read by the brokers) and `PROCURATOR_EDITION=x` to `.env` (passed into the gateway container by `compose.web.yaml`).
- A connector manifest may carry `"edition": "x"` (today: `integrations/web/connectors/builds.json`). `connectors.Registry.active()` drops those connectors on a standard box, so their broker actions refuse; the generated `connector-policy.json` marks their tools with `"edition": "x"`, and `connector-gate.mjs` refuses them unless `PROCURATOR_EDITION=x`.
- `capabilities.mjs` leaves the code-builds line out of the capability card on a standard box, so the model is never told about tools it cannot call; `builds.mjs` commands answer with the edition note.
- Switching later: re-run the installer with the other `--edition`; state is kept.

## Adding an X-only feature

1. Put `"edition": "x"` in the connector manifest; regenerate the policy (`python3 integrations/web/connectors.py policy …`, copy to the three plugins).
2. Make the feature inert on a standard box (gate the commands, drop the card line) and add a test for both editions (see `EditionTests` in `integrations/web/test_connectors.py` and the edition tests in `builds.test.mjs`, `capabilities.test.mjs`, `connector-gate.test.mjs`).
