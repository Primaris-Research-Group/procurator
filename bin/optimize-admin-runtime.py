#!/usr/bin/env python3
"""Build a conservative configuration patch; activation is a separate backed-up step."""
import copy


def build_config(current):
    config = copy.deepcopy(current)
    defaults = config.setdefault('agents', {}).setdefault('defaults', {})
    model = defaults.get('model', {}).get('primary')
    if model != 'anthropic/claude-sonnet-5-5':
        raise ValueError('Inspect the selected model before applying this runtime patch.')
    # Keep one foreground worker on the 2GB host; give a healthy tool loop time.
    defaults['maxConcurrent'] = 1
    defaults['timeoutSeconds'] = max(defaults.get('timeoutSeconds', 120), 600)
    defaults['userTimezone'] = 'America/New_York'
    params = defaults.setdefault('models', {}).setdefault(model, {}).setdefault('params', {})
    params.setdefault('cacheRetention', 'short')
    # Only expendable research/snapshot outputs are pruned, never approvals or receipts.
    if 'contextPruning' not in defaults:
        defaults['contextPruning'] = {'mode': 'cache-ttl', 'ttl': '5m',
            'tools': {'allow': ['web_browser_snapshot', 'web_fetch', 'web_search']},
            'hardClear': {'enabled': True, 'placeholder': '[Older read-only page result pruned. Inspect a fresh source; saved approvals and task receipts remain authoritative.]'}}
    compaction = defaults.setdefault('compaction', {})
    for key, value in {'mode': 'safeguard', 'identifierPolicy': 'strict',
                       'recentTurnsPreserve': 6, 'midTurnPrecheck': {'enabled': True}}.items():
        compaction.setdefault(key, value)
    tools = config.setdefault('tools', {})
    tools.setdefault('loopDetection', {})['enabled'] = True
    for name in ['admin_workflow', 'phone_read_call']:
        if name not in tools.setdefault('alsoAllow', []):
            tools['alsoAllow'].append(name)
    web = config['plugins']['entries']['procurator-web']
    # Fixed coordinator guidance only on main; phone prompts remain isolated.
    # The pinned gateway blocks typed before_prompt_build hooks of non-bundled
    # plugins unless allowConversationAccess is also set (observed Nov 3 log).
    web.setdefault('hooks', {}).update(allowConversationAccess=True, allowPromptInjection=True)
    return config
