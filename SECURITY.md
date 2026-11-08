# Security

## Reporting a vulnerability

Please report security issues privately through GitHub: **[Report a vulnerability](https://github.com/Primaris-Research-Group/procurator/security/advisories/new)** (Security tab › Report a vulnerability). Only the maintainers see the report. Do not open a public issue for a vulnerability. We aim to acknowledge within 3 days and to ship a fix or mitigation within 30.

## Supported versions

| Version | Supported |
|---|---|
| 0.30.x | yes |
| < 0.30 | no (pre-release working copies) |

## Threat model, in short

PROCURATOR acts on the owner's behalf with the owner's credentials, so the boundary that matters is **what the model can cause without the owner**:

- The model talks only to a broker over `tools.sock`; credentials live in the brokers (root, systemd) and never reach the model or the gateway container.
- Owner commands (approvals) arrive on a separate `approval.sock`; the model cannot write to it.
- Every tool is declared in a connector manifest with a risk class. Undeclared tools and undeclared broker actions refuse (deny by default). `external` and `public` tools run only after a Telegram approval bound to a hash of the exact content.
- Web pages, email bodies, documents and call transcripts are untrusted text: they are fenced before the model sees them and can never grant a permission.
- Health values are labeled at source and checked at every exit (mail, SMS, posts, documents); they go only to recipients a grant names.
- Hard blocks (`integrations/web/policy/hard-blocks.json`) are enforced in code: payments beyond the owner's cap, new accounts, messages to unknown numbers, deleting data.

Known limitations of 0.30.0 (tracked publicly as issues):

- Privilege on the sockets is by path and file mode, not by a per-boot credential (planned: H1).
- `/webtask` binds the first 16 hex characters of the content hash (planned: full hash server-side).
- The project has not been independently audited.

See `docs/security-model.md` for the full model.
