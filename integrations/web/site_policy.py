"""Per-site routing policy for the browser (Nov 5 2026, from the site access map).

Some sites forbid or block outside agents, and pushing a browser at them risks the owner's own accounts
(Amazon cut off Meta's Muse in Sept 2026; eBay suspended users; Chase, Schwab and Citi block agent logins;
Wells Fargo makes the customer liable for everything an agent does). The broker checks this table before a
browser opens and before any step is prepared, so the model cannot drift into those sites.

Modes:
  never          no browser at all (banks, brokerages, payment apps, Meta social sites, Google search pages)
  read_only      public reading only: open pages and follow links; no typing, choosing, buttons or saved logins
  owner_final    the bot may fill the cart/checkout, but the final Buy/Place order is pressed by the owner (takeover)
  browser        normal rules (task approvals, final reviews, purchase approval)
Owner overrides (/sitepolicy) can change any mode except the fixed financial "never" list.
"""
import json
import re

FINANCE = ('chase.com', 'jpmorganchase.com', 'wellsfargo.com', 'bankofamerica.com', 'citi.com', 'citibank.com', 'schwab.com',
           'capitalone.com', 'usbank.com', 'pnc.com', 'truist.com', 'ally.com', 'discover.com', 'americanexpress.com', 'fidelity.com',
           'vanguard.com', 'etrade.com', 'morganstanley.com', 'robinhood.com', 'paypal.com', 'venmo.com', 'cash.app', 'zellepay.com',
           'sofi.com', 'navyfederal.org', 'synchrony.com', 'mysynchrony.com', 'chime.com', 'tdbank.com', 'regions.com', 'huntington.com',
           'citizensbank.com', 'key.com', 'mtb.com', 'santanderbank.com', 'td.com', 'usaa.com', 'penfed.org', 'coinbase.com',
           'kraken.com', 'binance.us', 'marcus.com', 'goldmansachs.com', 'barclaysus.com', 'bmo.com', 'firstcitizens.com')
FIXED = {d: ('never', 'Banks, brokerages and payment apps: the assistant never signs in or browses them (several block agents, and '
             'some make you liable for anything an agent does). Use statements in email, or ask them to check themselves.') for d in FINANCE}
DEFAULTS = {
    **{d: ('never', 'Meta\'s terms forbid automating personal Instagram/Facebook accounts; risk of account suspension.') for d in
       ('instagram.com', 'facebook.com', 'messenger.com', 'threads.net', 'threads.com')},
    # 0.28.0 (plan v2 B7): social sites are reached through their official APIs only (social connectors, /postsend).
    **{d: ('never', 'Social sites are reached only through their official APIs (social_* tools with your /postsend approval); browser automation risks the account.')
       for d in ('tiktok.com', 'linkedin.com')},
    'google.com': ('never', 'Google blocks automated search pages. Use web_search, news_headlines or the Google tools instead.'),
    # Nov 5: both forbid automated access in their terms; OpenEvidence accounts are NPI-verified and can be suspended.
    'openevidence.com': ('never', 'OpenEvidence forbids bots and agents and can suspend their NPI-verified account. Use evidence_search; for OpenEvidence itself, write the exact question for them to paste into the app.'),
    'mdcalc.com': ('never', 'MDCalc forbids automated access. Use clinical_calc; for a score it lacks, name the MDCalc calculator for them to run themselves.'),
    'amazon.com': ('read_only', 'Amazon blocks outside shopping agents and can restrict the customer\'s account. Read product pages only; '
                   'for a purchase, send them the product link (or /webtakeover) so the owner buys it themselves.'),
    'ebay.com': ('read_only', 'eBay restricts agents and has suspended users\' accounts. Read listings only; the owner bids or buys themselves.'),
    'yelp.com': ('read_only', 'Yelp does not allow agent bookings or quotes without a paid license. Read only; book through the business\'s own site or by phone.'),
    **{d: ('read_only', 'Airlines block agent bookings (Delta, United terms). Compare fares by reading or with web_search; for booking, '
           'send them the flight details and link, or open a browser for them to finish with /webtakeover.') for d in
       ('delta.com', 'united.com', 'aa.com', 'southwest.com', 'jetblue.com', 'alaskaair.com', 'spirit.com', 'flyfrontier.com',
        'hawaiianairlines.com', 'allegiantair.com', 'suncountry.com', 'breezeairways.com')},
    'walmart.com': ('owner_final', 'Walmart shows a human-verification step agents cannot pass; prepare the cart, and the owner presses the final order button themselves.'),
    'target.com': ('owner_final', 'Prepare the cart; the owner presses the final order button themselves.'),
}
# 0.20.7 (Nov 7 batteries, W12): Google's SEARCH pages block bots, but Google Flights, Hotels and Maps work in a real browser
# and are the best fare/route sources, so those paths open normally while the rest of google.com stays off-limits.
PATH_RULES = {
    'google.com': (
        ('/travel/', 'browser', 'Google Flights and Google Hotels (read and compare; nothing is booked on Google).'),
        ('/maps', 'browser', 'Google Maps (places, hours, directions).'),
        ('/flights', 'browser', 'Google Flights (read and compare; nothing is booked on Google).'),
    ),
}
MODES = ('never', 'read_only', 'owner_final', 'browser')
DOMAIN = re.compile(r'^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$')


def match(host, table):
    host = (host or '').lower().rstrip('.')
    best = None
    for d in table:
        if host == d or host.endswith('.' + d):
            if best is None or len(d) > len(best):
                best = d
    return best


def policy(host, overrides=None, path=''):
    """Effective policy for a hostname (and, for a few sites, the page path): {'domain','mode','reason','source'}."""
    d = match(host, FIXED)
    if d:
        return {'domain': d, 'mode': 'never', 'reason': FIXED[d][1], 'source': 'fixed'}
    pr = match(host, PATH_RULES)
    if pr and path:
        for prefix, mode, reason in PATH_RULES[pr]:
            if path.lower().startswith(prefix):
                return {'domain': pr + prefix.rstrip('/'), 'mode': mode, 'reason': reason, 'source': 'default'}
    overrides = overrides or {}
    o = match(host, overrides)
    if o:
        return {'domain': o, 'mode': overrides[o], 'reason': 'Set by the owner (/sitepolicy).', 'source': 'owner'}
    d = match(host, DEFAULTS)
    if d:
        return {'domain': d, 'mode': DEFAULTS[d][0], 'reason': DEFAULTS[d][1], 'source': 'default'}
    return {'domain': host, 'mode': 'browser', 'reason': '', 'source': 'default'}


def set_override(overrides, domain, mode):
    domain = (domain or '').lower().strip().removeprefix('https://').removeprefix('http://').split('/')[0].removeprefix('www.')
    if not DOMAIN.match(domain):
        raise ValueError('Give a website like amazon.com.')
    if match(domain, FIXED):
        raise ValueError('%s is on the fixed list of banks and payment sites; the assistant never browses it.' % domain)
    out = dict(overrides or {})
    if mode == 'default':
        out.pop(domain, None)
    elif mode in MODES:
        out[domain] = mode
    else:
        raise ValueError('Mode must be one of: browser, read_only, owner_final, never, default.')
    if len(out) > 200:
        raise ValueError('Too many site rules.')
    return domain, out


def describe(p):
    return {'never': 'not browsed', 'read_only': 'read only (no typing, buttons or sign-in)', 'owner_final': 'you press the final order button',
            'browser': 'normal rules'}[p['mode']]


def dumps(overrides):
    return json.dumps(overrides, sort_keys=True)


# ---------- 0.28.0 (plan v2 B7): what the Live view may show ----------
# show: the frame as captured (fields below still masked); mask: sensitive fields masked; hide: no image, a placeholder only.
VIEWS = ('show', 'mask', 'hide')
HEALTH_PORTAL_HINTS = ('mychart', 'patientportal', 'followmyhealth', 'myhealth', 'healthelife', 'patient-portal', 'mock-ehr')
SIGNIN_PATH = re.compile(r'(?i)/(?:log-?in|sign-?in|signon|auth(?:orize)?|sso|account/(?:login|signin)|oauth2?/authorize|session/new)\b')
# Selectors the runner passes to page.screenshot({mask}) on every page (any view but hide).
MASK_SELECTORS = ('input[type=password]', 'input[autocomplete=current-password]', 'input[autocomplete=new-password]',
                  'input[autocomplete^=cc-]', 'input[autocomplete=one-time-code]', 'input[name*=card i]', 'input[name*=cvv i]',
                  'input[name*=cvc i]', 'input[name*=otp i]', 'input[name*=ssn i]')


def view(host, path='', health_hosts=(), overrides=None):
    """{'view': show|mask|hide, 'reason'} for the Live view of a page."""
    host = (host or '').lower().rstrip('.')
    if match(host, FIXED) or (match(host, DEFAULTS) and DEFAULTS[match(host, DEFAULTS)][0] == 'never'):
        return {'view': 'hide', 'reason': 'bank, payment or never-browsed site'}
    if match(host, set(health_hosts)) or any(h in host for h in HEALTH_PORTAL_HINTS):
        return {'view': 'hide', 'reason': 'health portal'}
    o = match(host, overrides or {})
    if o and overrides[o] in VIEWS:
        return {'view': overrides[o], 'reason': 'set by the owner'}
    if SIGNIN_PATH.search(path or ''):
        return {'view': 'mask', 'reason': 'sign-in page'}
    return {'view': 'show', 'reason': ''}

