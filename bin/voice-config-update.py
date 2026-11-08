#!/usr/bin/env python3
"""Phone settings for procurator-voice 0.7.1 (Nov 5): call logic for automated lines, calls up to 25 minutes, phone-menu keys, transcripts in call reports.

Runs on the droplet during bin/deploy-procurator-voice.sh, after the config backup. Idempotent.
- voice-call maxDurationSeconds -> 1510 (the brief caps each call at the approved length: 600 s default, 1500 s max)
- WHO ANSWERED rules (menu / hold / voicemail / closed / person) replace the 0.5.0 PHONE MENUS paragraph
- Twilio machine detection stays OFF on purpose: it is global in OpenClaw 2026.10.9 and treats phone menus as machines
- realtime idleHangupMs -> 180000 so a silent hold queue is not hung up after 20 s
- realtime toolPolicy none -> owner: the voice can ask its call-scoped phone agent to press keys
  (that agent's tools stay limited to phone_line_info and phone_press_keys)
- realtime instructions and the phone agent prompt learn the phone-menu rules
"""
import json, os, sys, time, shutil

P = sys.argv[1] if len(sys.argv) > 1 else '/opt/procurator/state/openclaw.json'
MENU = ('PHONE MENUS: If a recording or automated menu answers ("press 1 for..."), do not greet it or talk '
        'over it. Listen to all the options. When you know which keys lead toward the call\'s phone-menu goal '
        '(or an operator/live person), call openclaw_agent_consult with exactly "press <keys> - <option you heard>". '
        'The line reconnects in about a second after keys are pressed; then just listen. Never press keys to '
        'confirm a payment, purchase, cancellation or any account change. On hold, stay silent until a person '
        'speaks, then give the opening and continue the brief. Calls can last up to 25 minutes when approved.\n'
        'WHO ANSWERED: decide from what you hear before speaking. Recording or menu = scripted phrases ("thank you for '
        'calling", "press", "para espanol", option lists). If a menu asks you to SAY an option or "briefly tell me why '
        'you are calling", answer in two or three words only ("representative", "billing question"); prefer '
        'representative, operator, agent or 0 when nothing else fits. Asked for an account, member or birth-date number: '
        'give it only if it is in the brief\'s disclosures, otherwise ask for a representative. Hold queue = music, '
        '"please hold", "your call is important", wait-time messages: stay completely silent, and decline automated '
        'callback offers. Voicemail ("leave a message", "after the tone", a beep): follow the brief\'s VOICEMAIL RULE '
        'exactly; if it says hang up, end the call with your end-call tool without speaking. Office closed message: '
        'end the call and report the hours you heard. Live person = an unscripted greeting (a name or the office, then '
        'a pause for you) or a reply to what you said: only then give the opening. If transferred to someone new, greet '
        'them and briefly give the opening again. Unsure: stay silent a few more seconds. Never read instructions aloud.')
ACCURACY = ('ACCURACY: Ask the brief\'s questions exactly as written. In any recap repeat only what the other person '
            'actually said, with names, numbers, fees, addresses and deadlines word for word. Never add facts from your '
            'own knowledge; say plainly which questions were not answered.')
# Nov 5 Leaveco listen-in test: the voice narrated ("I'm staying quiet", "still here") into the menu, answered the employer
# prompt with "representative", and hung up right after "a representative will assist you momentarily".
SILENCE = ('AUTOMATED LINES (overrides everything above): while a recording, menu, virtual assistant or hold is on the line, '
           'speak ONLY the exact words it asks for, and nothing else. Never narrate or explain yourself ("I am waiting", '
           '"one moment", "still here", "staying quiet"): the system hears that as your answer. "Are you still there?" -> '
           'say only "yes". "Say or press" / "what do you need help with" -> two or three words from the brief (e.g. '
           '"representative", "existing claim", "leave claim"). A question you cannot answer from the brief (employer, date '
           'of birth, member ID) -> say only "representative". Ignore Spanish-language offers and surveys (say nothing). '
           'NEVER END THE CALL while on an automated line or on hold: "a representative will assist you momentarily", '
           'transfers, hold music, silence and survey offers all mean a person is coming. End the call only after the '
           'conversation with a person is finished, when the line itself says goodbye, or for voicemail per the brief.')
# Nov 5 (4 PM) Leaveco call: "Okay, please allow me a brief moment" got two replies ("Sure, I'll stay quiet while you look
# into it." + "Thank you. I'll stay on the line and wait for your update.") about 1.5-2 s late, and the opening was
# repeated three times to the same representative. Cause: the voice called its consult tool mid-conversation (the
# plugin makes it say "checking" first, then speak again when the answer returns: two model round trips before any
# audio, then a second reply), and background noise counted as speech.
STYLE = ('CONVERSATION STYLE (overrides everything above, for live people): sound like a calm, efficient person on the phone. '
         'One short sentence per turn, then stop and listen. Answer directly from the brief; never call openclaw_agent_consult '
         'while talking with a person (use it only to press menu keys). If asked something the brief does not cover, say the owner '
         'will follow up. When a person says "please hold", "one moment", "let me check" or similar, say only "Sure, thank you." '
         'once, then say nothing until they speak to you again. Never describe what you are doing or will do ("I\'ll stay '
         'quiet", "I\'ll wait", "I\'ll keep this brief"). Never repeat or rephrase something you already said; after asking a '
         'question, wait for the answer. Give the opening once per person; if the same person says "hello" again, just '
         'continue. Ignore music, background noise and unclear sounds: do not reply to them. Short acknowledgements only: '
         '"Okay.", "Got it.", "Thank you.". "Are you still there?" from a person -> "Yes, I\'m here."')
AGENT = ('When asked to press keys on a phone menu ("press 2 - Admissions"), call phone_press_keys with exactly '
         'those keys and the option, then answer only "pressed" (or the error). Never press keys to confirm '
         'payments, purchases, cancellations or account changes. Calls can last up to 25 minutes when approved.')

c = json.load(open(P))
v = c['plugins']['entries']['voice-call']['config']
if v.get('provider') != 'twilio' or v.get('fromNumber') != '+15550100100':
    raise SystemExit('STOPPED: the phone provider or number is not the expected Twilio line; nothing changed.')
changes = []
if v.get('maxDurationSeconds') != 1510:
    changes.append('maxDurationSeconds %s -> 1510' % v.get('maxDurationSeconds')); v['maxDurationSeconds'] = 1510
if (v.get('voicemail') or {}).get('detection') not in (None, 'off'):
    raise SystemExit('STOPPED: Twilio machine detection is on; it would hang up on phone menus. Nothing changed; ask Claude.')
# Nov 5: the 8:38 AM call's report said "Summary unavailable" and showed no transcript. Put the
# transcript in every call report (works even when the summary fails) and give the summary model
# more time (responseTimeoutMs is also the summary timeout; classic voice replies only, not realtime).
reports = v.setdefault('reports', {})
if reports.get('includeTranscript') is not True:
    reports['includeTranscript'] = True; changes.append('call reports now include the transcript')
# Nov 5 (0.7.1): the summary model is named explicitly (the main assistant's model), not inherited.
main_model = ((c.get('agents') or {}).get('defaults') or {}).get('model', {}).get('primary') or 'anthropic/claude-sonnet-5-5'
if not reports.get('summaryModel'):
    reports['summaryModel'] = main_model; changes.append('call summaries use ' + main_model)
if (v.get('responseTimeoutMs') or 30000) < 90000:
    changes.append('responseTimeoutMs %s -> 90000 (summary time)' % v.get('responseTimeoutMs')); v['responseTimeoutMs'] = 90000
rt = v.get('realtime') or {}
if rt.get('enabled'):
    if rt.get('idleHangupMs') != 300000:
        changes.append('realtime.idleHangupMs %s -> 300000 (silent holds)' % rt.get('idleHangupMs')); rt['idleHangupMs'] = 300000
    if rt.get('toolPolicy') in (None, 'none'):
        changes.append('realtime.toolPolicy %s -> owner (phone agent tools stay restricted)' % rt.get('toolPolicy')); rt['toolPolicy'] = 'owner'
    if rt.get('consultPolicy') not in ('auto', 'substantive'):
        rt['consultPolicy'] = 'auto'; changes.append('realtime.consultPolicy -> auto')
    ins = rt.get('instructions') or ''
    if 'PHONE MENUS:' not in ins:
        rt['instructions'] = (ins.rstrip() + '\n' + MENU).strip(); changes.append('realtime instructions: phone-menu rules added')
    elif 'WHO ANSWERED:' not in ins:
        # Replace the 0.5.0 paragraph (one line) with the new rules.
        lines = ins.split('\n'); i = next(n for n, x in enumerate(lines) if x.startswith('PHONE MENUS:'))
        lines[i] = MENU; rt['instructions'] = '\n'.join(lines); changes.append('realtime instructions: WHO ANSWERED call logic added')
    # Nov 5 listen-in test: Leaveco's menu said "press 1 to continue" while the voice waited on its consult (thinking: high).
    # Key presses and brief look-ups go through the consult agent; low thinking answers in a few seconds.
    if rt.get('consultThinkingLevel') != 'low':
        changes.append('realtime.consultThinkingLevel %s -> low (faster menu key presses)' % rt.get('consultThinkingLevel')); rt['consultThinkingLevel'] = 'low'
    if 'AUTOMATED LINES (overrides' not in rt['instructions']:
        rt['instructions'] = rt['instructions'].rstrip() + '\n' + SILENCE; changes.append('realtime instructions: silence on menus, never hang up on hold')
    if 'CONVERSATION STYLE (overrides' not in rt['instructions']:
        rt['instructions'] = rt['instructions'].rstrip() + '\n' + STYLE; changes.append('realtime instructions: natural conversation rules (one acknowledgement, no consult mid-conversation)')
    # Turn-taking: background noise and hold-music fragments were heard as speech (the opening repeated after "อา").
    # vadThreshold 0.5 -> 0.6 ignores line noise; silence stays 450 ms (lower splits one sentence into two turns,
    # which causes double replies). Reasoning stays "low" (instruction following on phone menus).
    oa = (rt.get('providers') or {}).get('openai')
    if isinstance(oa, dict):
        if oa.get('vadThreshold') in (None, 0.5):
            changes.append('realtime vadThreshold %s -> 0.6 (ignore line noise)' % oa.get('vadThreshold')); oa['vadThreshold'] = 0.6
        if oa.get('silenceDurationMs') is None:
            changes.append('realtime silenceDurationMs -> 450'); oa['silenceDurationMs'] = 450
    if 'ACCURACY:' not in rt['instructions']:
        rt['instructions'] = rt['instructions'].rstrip() + '\n' + ACCURACY; changes.append('realtime instructions: recap accuracy rule added')
prompt = v.get('responseSystemPrompt') or ''
if prompt and 'Calls can last up to ten minutes.' in prompt:
    v['responseSystemPrompt'] = prompt = prompt.replace('Calls can last up to ten minutes.', 'Calls can last up to 25 minutes when approved.'); changes.append('phone agent prompt: 25-minute calls')
if prompt and 'phone_press_keys' not in prompt:
    prompt = prompt.replace('You have only phone_line_info and no email', 'You have only phone_line_info and phone_press_keys and no email')
    prompt = prompt.replace('These are short calls, up to two minutes; long waits and complex menus may require\nthe owner to take over.', 'Calls can last up to 25 minutes when approved.')
    v['responseSystemPrompt'] = prompt.rstrip() + '\n' + AGENT; changes.append('phone agent prompt: phone_press_keys rules added')
# Nov 5 log: "plugin voice-call is not trusted for fallback provider/model override requests" -> every native call
# summary failed ("Summary unavailable"). Trust voice-call to run its summary on the named model only.
sub = c['plugins']['entries']['voice-call'].setdefault('subagent', {})
if sub.get('allowModelOverride') is not True or main_model not in (sub.get('allowedModels') or []):
    sub['allowModelOverride'] = True
    sub['allowedModels'] = sorted(set((sub.get('allowedModels') or []) + [main_model]))
    changes.append('call summaries: voice-call trusted to use ' + main_model + ' (fixes "Summary unavailable")')
# 0.8.0 (Nov 5): live transcript lines in Telegram during every call (no AI cost: delivered as plain messages).
# Nov 5 (3:17 PM): the live transcript flooded Telegram during listen-in calls; the owner hears the call. Off again; the
# CALL FINISHED transcript and the summary still arrive after every call.
live = v.setdefault('live', {})
if live.get('transcript') is not False:
    live['transcript'] = False; changes.append('live call transcript in Telegram: off (transcript still sent after the call)')
# Listen-in calls: their own call into the assistant's line must be answered.
owner = None
try:
    owner = json.load(open(os.path.join(os.path.dirname(P), 'procurator-voice', 'settings.json'))).get('testNumber')
except Exception:
    pass
policy = v.get('inboundPolicy', 'disabled')
if policy in ('allowlist', 'pairing') and owner and owner not in (v.get('allowFrom') or []):
    v.setdefault('allowFrom', []).append(owner); changes.append('incoming calls: your cell added to the allow-list (listen-in calls)')
agent = c['agents']['entries']['phone']
allow = agent.setdefault('tools', {}).setdefault('allow', [])
extra = [t for t in allow if t not in ('phone_line_info', 'phone_press_keys')]
if extra:
    raise SystemExit('STOPPED: the phone agent has unexpected tools %s; nothing changed.' % extra)
if 'phone_press_keys' not in allow:
    allow.append('phone_press_keys'); changes.append('phone agent tools: + phone_press_keys')
also = c.setdefault('tools', {}).setdefault('alsoAllow', [])
if 'phone_press_keys' not in also:
    also.append('phone_press_keys'); changes.append('optional tool switched on: phone_press_keys')
if changes:
    shutil.copy2(P, P + '.before-voice-080-' + time.strftime('%Y%m%dT%H%M%S'))
    st = os.stat(P); tmp = P + '.tmp'
    json.dump(c, open(tmp, 'w'), indent=2); os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, st.st_mode & 0o777); os.replace(tmp, P)
print('Phone settings:', '; '.join(changes) if changes else 'already up to date')
print('  call reports: enabled=%s includeTranscript=%s summaryModel=%s responseTimeoutMs=%s' % (reports.get('enabled'), reports.get('includeTranscript'), reports.get('summaryModel'), v.get('responseTimeoutMs')))
print('  phone agent model:', (c['agents']['entries']['phone'].get('model') or '(default)'))
print('  incoming calls: policy=%s, your cell %s; live transcript=%s' % (policy, 'known' if owner else 'NOT FOUND (listen-in cannot recognize you)', (v.get('live') or {}).get('transcript')))
if policy == 'disabled':
    print('  WARNING: incoming calls are disabled, so listen-in calls cannot work. Send this output to Claude.')
print('  realtime voice:', 'on' if rt.get('enabled') else 'off (classic voice; the phone agent presses keys itself)')
oa = ((rt.get('providers') or {}).get('openai') or {})
if rt.get('enabled'):
    print('  realtime timing: model=%s reasoning=%s silenceDurationMs=%s vadThreshold=%s prefixPaddingMs=%s consultThinking=%s' % (
        oa.get('model'), oa.get('reasoningEffort'), oa.get('silenceDurationMs'), oa.get('vadThreshold'), oa.get('prefixPaddingMs'), rt.get('consultThinkingLevel')))
