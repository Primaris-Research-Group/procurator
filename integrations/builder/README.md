# PROCURATOR builders (code builds on the owner's Windows PC and Mac)

Built Nov 5, 2026. PROCURATOR writes a build packet, and the owner approves the exact packet in Telegram. A builder then runs Claude Code or Codex using their own subscriptions and opens a **draft** pull request. The builders are:

- **windows**: the owner's always-on desktop (Ryzen 5, 16 GB), running inside WSL2 Ubuntu
- **mac**: the owner's Mac

Nothing is merged or deployed automatically.

```
Telegram ──▶ main agent: build_prepare ──▶ BUILD REVIEW (who can run it) ──▶ /buildapprove ID CODE
                                                             │ queue/<id>.json (droplet)
builder worker (Windows PC in WSL / Mac) ──ssh, its own restricted key──▶ build-relay.py --builder NAME: hello · pull · report
      │ fresh clone → branch procurator/<id> → claude -p / codex exec → checks → repo tests (that machine's config)
      └▶ git push branch → gh pr create --draft ──▶ report(done, PR link) ──▶ Telegram "BUILD READY"
```

## Live builds (worker 1.4.0, Nov 8)

A build is no longer a black box. While the agent works:

- **Progress streams back.** Claude Code runs with `--output-format stream-json --input-format stream-json`; Codex with `codex exec --json`. Every few seconds (`progress_s`) the worker reports what the agent said, which files it touched and which commands it ran; the relay appends it to `progress/<id>.jsonl` and the plugin turns it into Telegram digests, `/buildlog` and the dashboard's live log.
- **The owner steers.** `/buildnote ID text` is queued by the plugin under `notes/<id>/`; the relay hands each note to the worker once, with the answer to its next report; the worker sends it to the agent as a user message that starts with `OWNER:` (Codex: a new `codex exec resume --last` round).
- **The agent can ask.** The prompt tells it to reply with one line `QUESTION: …` and wait when it needs a decision. The worker reports `waiting_owner` with the question, the owner gets a BUILD QUESTION message at once, and their note resumes the agent. Waiting does not count against the time limit; after `wait_minutes` (default 10) the agent is told to use its judgement and finish.
- **The packet may name the model** (`model`), passed as `claude --model` / `codex -m`. The builder can restrict it (`models`) and set defaults (`default_models`) in `config.json`.

Config keys: `live` (true; false restores the one-shot run), `wait_minutes`, `progress_s`, `max_rounds`, `models`, `default_models`. Nothing about pushing, path or secret checks changed: the agent still cannot push, and every change is checked before the branch leaves the machine.

## Verified pull requests (worker 1.5.0, Nov 8)

When the agent says it is done, `Job.verify` runs three gates and sends failures back to it as `VERIFICATION:` messages (up to `verify_rounds`, default 2):

1. **Acceptance checks**: the prompt makes the agent write `.procurator/checks/NN-<slug>.sh` for each DONE WHEN item; `run_checks` runs them (timeout `check_timeout_s`); failed or missing → feedback.
2. **Tests**: the repo's test command from this machine's config; failure → feedback.
3. **Independent review** (`review_diff`): the other installed agent (`review: auto`), or the one the packet names, reads the staged diff read-only (Claude Code: `--permission-mode default`, Read/Grep/Glob/git diff only; Codex: `--sandbox read-only`) and answers one JSON object (`approve` | `request_changes`, summary, issues). `request_changes` → feedback once; then the review stands.

After the gates: the usual path/secret/size checks, commit, optional **screenshots** (`screenshots`: the repo's `preview` config starts the app, `url_up` waits for it, headless Chrome from `tools.chrome` or a common path captures each `paths` entry into `.procurator/evidence/<id>/`, committed), push, draft PR whose body (`pr_body`) opens with a Verification section. The report carries a size-limited `verification` object (checks, review, rounds, unresolved, screenshots, passed) which the relay validates and the plugin shows.

**Long-horizon packets:** `track_pr` records every PR this builder opens in `prs.json`; `pr_states` re-checks open ones with `gh pr view` every 5 minutes for 21 days and the result rides in `hello` (`prs`); the relay writes `pr_state` / `merged_at` into the build's report, the plugin tells the owner about merges, and packet steps `awaiting merge:<build id>` become eligible.

## Which builder runs a build

The packet's `builder` field decides:

- **`any`** (the default): the first builder that is online and has the repo on its own allow-list. That is normally the Windows PC, which is always on.
- **a builder name** (`"windows"` or `"mac"`): used when the owner asks for a specific machine.

The BUILD REVIEW shows which machines can run the build and whether they are online. An approved build waits up to 24 hours for a builder and then expires.

## Safety design

- **Builders pull work; the droplet never pushes it.** Each builder's `authorized_keys` line is `restrict,command="…/build-relay.py --builder NAME"`.
  - Its identity comes from the key, not from anything the builder sends.
  - A builder can only claim builds meant for it (or for `any`) whose repo is on its own list.
  - A builder can only report on builds it claimed.
- **Each machine's own allow-list decides.** The repos and test commands live in `~/Procurator/builder/config.json` on each machine, never in the packet.
- **Exact approval.** `/buildapprove ID CODE` binds to a hash of the packet. A packet waits 30 minutes for approval; an approved build waits 24 hours for a builder.
- **The agent cannot push.**
  - While the agent works, the push URL points nowhere.
  - Claude Code runs with `acceptEdits` and a short Bash allow-list. It is denied `git push`, `git remote`, `git config`, `ssh`, `scp`, `curl`, `wget`, `gh` and WebFetch.
  - Codex runs with `--full-auto`, which keeps it in its workspace sandbox with no network.
- **Checks before any push.** The change is blocked if:
  - it touches a file outside the allowed paths;
  - it adds a secret file (`.env*`, keys, `.npmrc`, `credentials*.json`, …);
  - it adds anything that looks like a key or token;
  - it changes more than 200 files or 20,000 lines.
- **The builder's own commits are plain.** They run without signing, and repository hooks never run.
- **Restarts and duplicates are handled.**
  - A build that was running when its machine restarted is reported as failed, not left hanging.
  - Only one worker runs per machine (a lock file enforces this).
- **WSL uses Linux tools only.** The builder ignores Windows programs that are visible from WSL (`/mnt/c/...`).

## Files

| Where | File | Role |
|---|---|---|
| Builder | `~/Procurator/builder/worker.py` | Background loop and CLI: `check`, `add-repo`, `remove-repo`, `list` |
| Builder | `~/Procurator/builder/config.json` | Name and label, allowed repos (branch, test command, agents), tool paths, relay command |
| Mac | `~/Library/LaunchAgents/org.primaris.procurator-builder.plist` | Keeps the Mac worker running while the owner is logged in |
| Windows | Task Scheduler entry "PROCURATOR builder" | At sign-in, runs `wsl.exe … worker.py run` hidden and restarts it if it stops. Fallback if Task Scheduler refuses: a hidden starter in the Startup folder |
| Builder | `~/Procurator/builds/<id>/` | Clone, `prompt.md`, `agent.log`, `tests.log`, `pr-body.md` (kept 7 days) |
| Droplet | `/opt/procurator/bin/build-relay.py` | Forced command, one key line per builder |
| Droplet | `state/procurator-build/` | Plugin: `jobs.json`, `queue/`, `cancel/`. Relay: `claims/`, `reports/`, `builders/<name>.json` |
| Gateway | `integrations/web-plugin/builds.mjs` | Tools `build_prepare` (with `builder`), `build_status`, `build_cancel`; commands `/buildapprove`, `/buildcancel`, `/buildstatus`, `/buildreview`; 30-second sweep sends Telegram notices and expires stale builds |
| Repo | `integrations/builder/dist/install-procurator-builder-wsl.sh` | One-file Windows installer with worker.py embedded. Rebuild it with `python3 integrations/builder/bundle.py` |

## Setup

**Mac** (from the deploy folder):
```
bash bin/setup-builder.sh
python3 ~/Procurator/builder/worker.py add-repo OWNER/NAME --test "npm test"
```

**Windows PC:**
1. If WSL isn't installed yet, open PowerShell as Administrator and run `wsl --install -d Ubuntu`, restart the PC, then open Ubuntu and create your Linux user.
2. Inside Ubuntu, install Claude Code and/or Codex and sign in. Also run `gh auth login` and set `git config --global user.name` and `user.email`.
3. Inside Ubuntu, run `bash /mnt/c/Users/<you>/Downloads/install-procurator-builder-wsl.sh`. It prints a command.
4. On the Mac, run the printed command: `bash bin/setup-builder.sh --enroll windows 'ssh-ed25519 …'`. It waits for the PC's first check-in.
5. Inside Ubuntu, allow repos: `python3 ~/Procurator/builder/worker.py add-repo OWNER/NAME --test "npm test"`.

**Gateway side** (once): `PROCURATOR_SKIP_CALL_CHECK=1 bash bin/deploy-procurator-web.sh 2>&1 | tee deploy-web.txt`

**Remove:**
- Windows: inside Ubuntu, `bash install-procurator-builder-wsl.sh --remove`; then on the Mac, `bash bin/setup-builder.sh --uninstall windows`.
- Mac: `bash bin/setup-builder.sh --uninstall`.

## Tests

- `cd integrations/builder && python3 -m unittest -q` runs 10 end-to-end tests using a fake gh, fake agents and a local git remote. They cover:
  - the relay contract;
  - Claude and Codex builds;
  - blocked changes never being pushed;
  - unknown repos never handed out;
  - per-builder isolation;
  - restart recovery;
  - the single-instance lock;
  - cancel;
  - the CLI.
- `cd integrations/web-plugin && node --test builds.test.mjs` runs 8 tests. They cover:
  - validation with one and two builders;
  - review, approval and expiry;
  - the queue file handed to the relay;
  - Telegram notices;
  - forged PR links being ignored;
  - 24-hour expiry;
  - cancel;
  - owner-only commands.

## Limits

- **When builders run:**
  - The Windows builder runs while Windows is signed in; a locked screen is fine. After a Windows Update restart, sign in once.
  - The installer sets "sleep when plugged in" to never.
- **One build at a time per machine.**
- **CLI flags:** the `claude_args` and `codex_args` values in `config.json` match the 2026 CLIs. If a CLI changes, edit them there; no redeploy is needed.
- **Failing tests:** if tests fail, the build still opens a draft PR marked "tests FAILED". Set `push_when_tests_fail: false` to stop that.
