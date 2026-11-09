# Code builds (PROCURATOR-X): write a packet, pick a model, steer it live, get a verified pull request

PROCURATOR-X turns a plain Markdown packet into a **draft pull request** on your own Mac or PC. The coding agent (Claude Code or Codex, on your own subscription) works in a fresh clone with pushing disabled; the server never runs commands on your machine, and nothing is merged or deployed by itself.

Since 0.31.0 a build is interactive: you follow it in Telegram, the agent's questions reach you, and your replies steer it. Since 0.32.0 every pull request arrives **verified**: the agent's own acceptance checks and the repo's tests have run, a second agent has reviewed the diff, and (when configured) screenshots of the running app are attached.

## 1. Write a BUILD.md

```markdown
---
repo: your-org/your-repo
base: main            # optional, default: the builder's branch for this repo
agent: claude         # claude (Claude Code) or codex
model: opus           # optional: what the agent's CLI accepts (opus, sonnet, a full id; codex: e.g. gpt-5.3-codex)
review: auto          # optional: who reviews the diff before the PR opens — auto (the other agent), claude, codex, off
review_model: ''      # optional: the reviewer's model
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

## Do not touch
- things the agent must leave alone (files, behaviour, dependencies)

## Notes
Anything else the agent should know: conventions, why.
```

Only the title and the goal are required; everything else has a default. A copy of this template is in `config/BUILD.example.md`.

**Write acceptance items a script can check.** Each one becomes a check the builder runs ("GET /healthz returns 200", "tests pass", "README contains a badge"); "it works well" cannot be checked and the Composer flags it.

### Or let the Composer interview you

On the dashboard's **Compose** tab, say what you want built. For coding work the Composer asks five things in order: the repo and the change in one sentence; which folders may change and what must not be touched; acceptance items it can check; model, reviewer and time limit (with defaults); anything the agent must know. It renders the BUILD.md for you (the **Preview** button shows it), saves it as a draft packet, and gives you one Telegram command, `/buildfrom <slug>`, that sends the BUILD REVIEW.

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

## 4. Verification before the PR opens

When the agent says it is done, the builder runs three gates in order. Anything that fails is sent back to the agent as a `VERIFICATION:` message, up to `verify_rounds` times (default 2), before the PR opens:

1. **Acceptance checks.** The prompt requires the agent to write one executable script per acceptance item in `.procurator/checks/` (`01-<slug>.sh`, `02-…`), exiting 0 when the item holds. The builder runs them (timeout `check_timeout_s`); a failing or missing check goes back to the agent.
2. **Tests.** The repo's test command from the builder's own config, as before; a failure goes back to the agent.
3. **Independent review.** A second agent reads the staged diff with read-only tools and answers `approve` or `request_changes` with a summary and issues. `review: auto` picks the *other* installed agent (Claude Code writes, Codex reviews, or the reverse), so the reviewer has not seen the work in progress; `claude` / `codex` force one, `off` skips it. A `request_changes` verdict goes back to the implementer once, then the review stands in the PR as it is.

Then the builder checks the change itself (allowed paths, no secret files or keys, size limits), commits, and, if the repo has a `preview` setting on the builder, starts the app and captures **screenshots**:

```json
"repos": {"your-org/your-repo": {"branch": "main", "test": "npm test",
          "preview": {"command": "npm run dev", "url": "http://127.0.0.1:3000", "paths": ["/", "/settings"], "ready_s": 30}}}
```

Screenshots are taken with headless Chrome (`tools.chrome` in the builder config, or a common install path), committed under `.procurator/evidence/<build id>/` on the branch, and embedded in the PR body. Delete the folder before merging if you do not want it in history.

The branch `procurator/ID` is pushed and a **draft** PR opens with a **Verification** section at the top: an acceptance table (✅ / ❌ per item), the test result, the review with its issues, and the screenshots; then the goal and the agent's summary. **BUILD READY** in Telegram summarises the same, and says plainly when there are open items. Nothing is merged or deployed by itself.

## 5. Chains of builds (long-horizon packets)

A task packet can hold several build steps. Each build starts from the base branch as it is *at that moment*, so a step that builds on an earlier step's code waits for that pull request to be merged: the packet runner marks it `awaiting merge:<build id>`, the builder polls its pull requests every five minutes (`gh pr view`, for three weeks), and the step becomes eligible by itself once you merge. Telegram tells you when a build's PR was merged or closed. The later build's goal carries a "Previous steps" list with the earlier PR links, so the agent knows what already landed.

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
- Check scripts are repository code written by the agent and run on your machine, like the test command. They run from the clone, with the time limit above; the reviewer is asked to flag a check that cheats. The reviewer itself runs read-only (Claude Code: Read/Grep/Glob and `git diff`; Codex: `--sandbox read-only`).

## Builder settings for live and verified builds

```json
"live": true,          // false = the old one-shot run (no progress, no notes)
"wait_minutes": 10,    // how long a QUESTION waits for you
"progress_s": 12,      // how often progress is reported
"max_rounds": 8        // how many question/answer or note rounds one build may have
```

```json
"checks": true,                                 // run the agent's acceptance checks
"review": {"agent": "auto", "model": ""},       // default reviewer when the packet names none
"verify_rounds": 2,                             // how many times failures go back to the implementer
"check_timeout_s": 120,
"tools": {"chrome": ""}                         // path to Chrome/Chromium for screenshots (empty = look in the usual places)
```

Claude Code runs with `--output-format stream-json --input-format stream-json`; Codex with `codex exec --json` and `codex exec resume --last` for each further round. Update `worker.py` on each builder to 1.5.0 (`bash bin/setup-builder.sh` on the Mac; re-run the WSL installer on Windows).
