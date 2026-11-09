# Code builds (PROCURATOR-X): write a packet, pick a model, steer it live

PROCURATOR-X turns a plain Markdown packet into a **draft pull request** on your own Mac or PC. The coding agent (Claude Code or Codex, on your own subscription) works in a fresh clone with pushing disabled; the server never runs commands on your machine, and nothing is merged or deployed by itself.

Since 0.31.0 a build is interactive: you follow it in Telegram, the agent's questions reach you, and your replies steer it.

## 1. Write a BUILD.md

```markdown
---
repo: your-org/your-repo
base: main            # optional, default: the builder's branch for this repo
agent: claude         # claude (Claude Code) or codex
model: opus           # optional: what the agent's CLI accepts (opus, sonnet, a full id; codex: e.g. gpt-5.3-codex)
builder: any          # optional: a builder name when you want a specific machine
paths: [src/, tests/] # optional: only these may change
max_minutes: 45       # 5–60, default 30
---
# One-line title for the pull request

## Goal
What to build or fix, precisely. Link the files, name the behaviour, give examples.

## Acceptance
- [ ] what must be true when it is done
- [ ] tests pass

## Notes
Anything else the agent should know: conventions, things not to touch, why.
```

Only the title and the goal are required; everything else has a default. A copy of this template is in `config/BUILD.example.md`.

## 2. Hand it to the assistant

Paste the file into Telegram ("Build this:" and the packet), or upload it to a task packet and say which step it belongs to. The assistant calls `build_prepare` with the Markdown and you get a **BUILD REVIEW**: repo, base, agent and model, who will run it, the goal, the checks, the files it may change, the time limit, and the one approval line.

Approve with the whole line (a copy button is under the message):

```
/buildapprove ID CODE
```

The code is a hash of the exact packet. A changed packet needs a new review.

## 3. Follow it live

- **Progress.** Every couple of minutes you get a short digest: what the agent said, which files it edited, which commands it ran. `/buildlog ID` shows the last twenty entries at any time; the dashboard's Builds page shows the same live log.
- **Steer.** `/buildnote ID your message` reaches the agent within about fifteen seconds as an instruction from you ("OWNER: …"). Use it to change course, add a fact, or say "stop and summarise".
- **Questions.** When the agent needs a decision it stops and sends `QUESTION: …`; the build shows as *waiting for you* and you get a **BUILD QUESTION** message at once. Answer with `/buildnote ID your answer`. If you do not answer within the builder's limit (default 10 minutes), the agent is told to use its judgement, keep the change minimal and finish; the time spent waiting does not count against the build's limit.
- **Stop.** `/buildcancel ID` stops the agent; nothing is pushed.

When the agent is done the builder checks the change (allowed paths, no secret files or keys, size limits), runs the repo's test command from that machine's own configuration, pushes the branch `procurator/ID`, opens a **draft** PR and sends you **BUILD READY** with the link, the test result and the agent's summary.

## Models

- The `model` field is passed to the agent's CLI as given (`claude --model …`, `codex -m …`).
- Each builder can restrict what it accepts and set defaults in `~/Procurator/builder/config.json`:

```json
"models": {"claude": ["opus", "sonnet"], "codex": ["gpt-5.3-codex"]},
"default_models": {"claude": "sonnet", "codex": ""}
```

  An empty list accepts anything the CLI accepts; an empty default lets the CLI choose.

## How steering stays safe

- Notes travel owner → plugin (`notes/<id>/`) → relay → builder, and only while that build is running. The model cannot write a note: `/buildnote` is a Telegram command checked against the owner, not a tool.
- The agent sees each note prefixed `OWNER:`; repository files, issues and web pages are never instructions to it.
- The builder's rules do not change with a note: pushing stays disabled while the agent works, the path, secret and size checks run before any push, and the test command comes from the machine's own config.
- Progress text is capped (400 characters per entry, 400 entries per build) and lives on the server as `state/procurator-build/progress/<id>.jsonl`; the agent's full log stays on the builder in `~/Procurator/builds/<id>/agent.log`.

## Builder settings for live builds

```json
"live": true,          // false = the old one-shot run (no progress, no notes)
"wait_minutes": 10,    // how long a QUESTION waits for you
"progress_s": 12,      // how often progress is reported
"max_rounds": 8        // how many question/answer or note rounds one build may have
```

Claude Code runs with `--output-format stream-json --input-format stream-json`; Codex with `codex exec --json` and `codex exec resume --last` for each further round. Update `worker.py` on each builder to 1.4.0 (`bash bin/setup-builder.sh` on the Mac; re-run the WSL installer on Windows).
