# Phone integration — approved assignments and message-taking

The dedicated Twilio line is **+1 555 010 0100**. The pinned gateway and official
`@openclaw/voice-call` plugin remain at 2026.10.9. The deployed approval plugin is
`procurator-voice` 0.3.1, including the general read-only call evidence tool. Source
also contains the realtime candidate; that transport remains inactive pending API
funding and migration acceptance. The private
API key is connected. No raw calling tool is available to the model.

## Owner workflow

In Telegram, describe the contact, exact telephone number, purpose, facts the
assistant may share and any exact nonfinancial scheduling permission. The main
assistant uses `phone_prepare_call`, then `phone_review_call`. It asks for missing
facts first. Preparing or delivering a review never places a call.

The private review shows destination, opening, task, context, disclosures,
permitted commitments, success criteria and duration. Send its entire native
`/callapprove ID HASH` line to approve that exact revision within ten minutes.
Changes require a new revision and fresh approval. `/callreview ID` re-shows a
review, `/callcancel ID` cancels a pending call, `/callstatus ID` checks a receipt,
and `/callend ID` requests hangup. `/calltest` retains the original owner-number
connection test. Never replay owner commands as the bot or bypass native checks.

Outgoing assignments accept geographic numbers in the 50 US states and DC,
validated against a pinned [NANPA table](https://reports.nanpa.com/public/npa_report.csv)
from November 3, 2026. Unknown/new area codes, foreign NANP territories, premium,
toll-free and the assistant's own number are rejected. Area codes are not an
ownership, tariff or reachable-number check. Review the exact recipient yourself.
Calls are 30–120 seconds; the provider conversation timer is ten seconds shorter
than the reviewed maximum to leave answer headroom. There are at most 20 assignment
attempts per UTC day and one simultaneous call. Long holds and complex menus are
not automated. DTMF/menu control, transfers and retries are not exposed.

Payments, purchases, contracts, account access, credentials and further calls are
unavailable. Only expressly reviewed nonfinancial commitments may be discussed.
These conversational restrictions guide the model; they are not a guarantee of
perfect spoken compliance. Infrastructure enforces approval, destination, replay,
session isolation, tool access and configured limits. A statement or call receipt
is not evidence that a filing, reservation or other task succeeded.

## Incoming reception and reports

Incoming callers may leave a name, callback number, reason and preferred callback
time. The greeting identifies the assistant as AI and discloses transcription and
private summarization. It reads back the message without promising a callback time.
No incoming caller, including a matching owner phone number, can grant tasks,
access private accounts or inherit the owner's Telegram context.

`procurator-voice-http.service` remains an unprivileged, credential-free, loopback-only
answer bridge. It forwards the unmodified callback to the native signature-verifying
gateway before changing the initial pause-only answer. Open reception additionally
requires durable SQLite admission: 20 accepted calls per UTC day, three per caller
number. Replays reuse the saved decision. Caller IDs are not authenticated identity.
Over-limit calls receive a busy rejection. One week of admission identifiers,
caller digests and decisions is retained, pruned on later admission checks. Errors
fail closed. The service can write only its admission subdirectory, not its
root-owned settings or credentials.

The native provider generates a private Telegram report after a call, using only
its brief and transcript. Full transcripts are not included in Telegram by default.
The report route is the verified owner conversation `agent:main:main`, not a caller
or arbitrary model-supplied session. Reports contain untrusted third-party claims;
they cannot authorize actions. Summarization or delivery can fail independently of
the call. Inspect actual delivery receipts before claiming a report was received.

Audio recording remains off. Twilio processes speech; Anthropic processes the
conversation and summary; Telegram carries private reviews and reports. Private
call transcripts, briefs and attempt receipts remain in server state without an
automatic deletion schedule. Root credentials remain under `/var/lib/procurator-voice`.
The voice agent has only `phone_line_info`, memory lookup off and per-call sessions.
No email, Drive, browser, shell or owner-memory tools are enabled for it.

The main coordinator can use `phone_read_call` to read a matching ended outbound
call's available summary and bounded transcript. It checks the owner-approved
receipt, destination and isolated phone session and does not generate a model
summary, send a message or redial. Speech remains untrusted evidence, not new
authority. This enables a later document or website step to use actual call
findings; ended connection status alone never establishes task success.

## Classic voice compatibility

This is turn-based Twilio Gather/Polly speech with the existing Anthropic API,
not full-duplex realtime audio. The pinned classic provider stores call briefs
but does not apply them to its conversation prompt. The local `before_prompt_build`
hook adds an approved brief only to its exact active `agent:phone:voice:assignment:ID`
session. Pending, unrelated, corrupt and terminal records cannot supply a brief.
No model input or tool argument chooses the session binding. Ordinary incoming
sessions receive only the message-taking policy. The harmless `phone_line_info`
tool supplies the same session-bound information if needed.

The pinned classic provider initially emits a leading 30-second Pause for inbound
calls, delaying answer and preventing its greeting. The bridge changes only a
signature-accepted, initial pause-only response for the assigned line into an AI
connection announcement plus a POST Redirect. Operational, status, speech, DTMF,
streaming, rejected and failed responses pass through. See Twilio's
[Pause](https://www.twilio.com/docs/voice/twiml/pause) and
[Redirect](https://www.twilio.com/docs/voice/twiml/redirect) documentation.

## Validation and acceptance

- Original incoming human conversation passed November 3. Carrier duration was
  62 seconds including the answer announcement. Original outgoing native approval
  and placement passed, but the owner's phone screened it; outgoing human speech
  is still unverified. Both tests created zero audio recordings.
- 21 approval/assignment checks, 15 bridge/admission checks and four configuration
  checks pass locally. Credential helper's prior 15 checks are unchanged.
- New assignment conversation, public reception, duration expiry and actual report
  delivery need their live acceptance results; mocked tests do not establish them.
- The restarted gateway is healthy, Telegram polling and voice runtime are connected,
  all supporting services are active, and unsigned public webhook POSTs return 401.
  Actual Anthropic probes passed incoming message-taking and report generation with
  missing information preserved. The private owner report route was verified. No call
  was placed by these probes. This does not establish general unattended readiness.

After plugin/config changes, restart the gateway and wait for health and Telegram
polling before issuing a new review. Hot replacement can leave native commands
bound to a retired registry generation. Preserve those protections; do not bypass
owner-current checks. Persist an attempt before calling; ambiguous placement is
held and never retried. A hangup request does not free the local slot until terminal
status is reconciled with the matching native receipt.

Metered Twilio/Anthropic usage and taxes apply. Count and duration limits are not
monetary caps. The earlier planning example for 100 minutes and 400 recognition
turns was about $11–12 in Twilio costs plus Anthropic and taxes; see the previously
reviewed [Twilio pricing](https://www.twilio.com/en-us/voice/pricing/us). Actual cost
depends on carrier, synthesis and recognition usage.


## Readable status and report repair

`/callstatus ID` now shows a plain-language result, local Eastern timestamps,
connected duration and actual report-delivery state. Carrier UUIDs, raw epoch
values and nested JSON are retained internally. Completion of a connection is
never described as completion of an administrative task.

The rehearsal report failed because the original archive install was not accepted
as a trusted official source. Both summary-model override and Gateway delivery
were refused by the host. The exact 2026.10.9 package was reinstalled from its
canonical official npm spec; runtime inspection now reports `trusted-official`,
with the same verified SHA-512 integrity. No provenance checks were disabled.

`/callreport ID` recovers one completed, owner-approved call summary without
redialing. A bounded, read-only compatibility reader for the pinned event store
checks the receipt, destination, phone agent and exact session before selecting
any transcript. The isolated phone agent summarizes only that brief/transcript,
and the verified owner route receives the result. A private report receipt
prevents automatic resend after ambiguous delivery. Repeating retrieval of an
already-sent recovered report returns its cached text. The original rehearsal
summary was recovered and its readable status delivered to the owner in Telegram.
Native automatic report delivery on a future call still needs a live check.

## Realtime migration candidate (inactive)

The owner requested lower latency and more natural speech. Measured classic AI
response intervals were 3.133 and 4.786 seconds before audio was queued. The host
has one CPU and about 2 GiB RAM; memory headroom is tight, although a short idle
sample had no active paging. A server resize was not performed for this repair.

The staged candidate uses OpenAI `gpt-realtime-2.1`, Marin voice, low reasoning,
450-ms endpoint silence, per-call briefs, call-bound hangup and no agent-consult
or private-context access. Anthropic remains the private summary engine. The
candidate is not evidence of real audio latency or subjective voice quality.

Run `python3 /opt/procurator/bin/connect-openai-voice.py` in the
assistant droplet's interactive root console. Hidden key entry performs a GET
model-access check and saves credentials privately; it does not start audio or
change routing. Model visibility does not itself prove funding or realtime
session acceptance. Do not put the key in chat, shell history or screenshots.

The operator then uses the staged `activate-realtime-voice.py --apply` workflow.
Before changing the working service, it runs a short synthetic audio preflight
using the configured model, voice and telephone audio format. This uses test text,
incurs metered API usage, and places no telephone call. Credentials are supplied
through private stdin, never command arguments or output. Model access without
successful audio does not permit activation.
It checks for active/unresolved calls, retains a rollback backup, cancels old
reviews, changes the immutable review's voice-processing snapshot, preserves
reception admission for realtime Connect/Stream replies, adds only the
single-use-token websocket route and recreates the gateway to load the private
environment key. Wait for health and Telegram startup. Verify unauthorized stream
requests fail and obtain a fresh native approval for a rehearsal. No real board
call is authorized by an API key, configuration activation or test receipt.

### November 3 billing check and rollback

The private model-access check passed, but an actual Realtime WebSocket session
returned `credit_balance_exhausted` before session acceptance or audio generation.
The attempted realtime configuration was restored from the private pre-migration
backup. Classic phone mode remains selected; the key and 0.3.0 candidate are retained.
The activation helper now checks actual audio before it changes the working service.
Add credits to the API organization associated with the saved key, then repeat that
preflight. A successful preflight still needs an owner-approved phone rehearsal to
verify real audio transport, voice quality, interruptions and automatic reporting.

The prior DRB brief remains a procedural inquiry about the District IV refusal to
docket fee arbitration. The letter receipt date is unconfirmed. The real call
review was withdrawn in favor of a rehearsal; no current real-call approval exists.
Ask about reviewing office, deadline and trigger, forms, filing method and service,
and request written instructions. Do not assume a deadline remains open. The bot
must obtain permission for AI participation and transcription before case questions.
