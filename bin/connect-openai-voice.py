#!/usr/bin/env python3
"""Save and check an OpenAI voice API key privately. Does not enable calls."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import warnings

STATE = Path('/var/lib/procurator-voice')
BASE = Path('/opt/procurator')
MODEL = 'gpt-realtime-2.1'


class SetupError(RuntimeError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'sk-[A-Za-z0-9_-]{20,500}', key):
        raise SetupError('The API key format was not recognized. Copy the secret key, not a project or organization ID.')


def verify_key(key, opener=None):
    validate_key(key)
    req = urllib.request.Request('https://api.openai.com/v1/models/'+MODEL,
        headers={'Authorization': 'Bearer '+key, 'Accept': 'application/json'}, method='GET')
    try:
        client = opener or urllib.request.build_opener(NoRedirects())
        with client.open(req, timeout=25) as response:
            raw = response.read(65537)
        if len(raw)>65536 or json.loads(raw).get('id') != MODEL:
            raise SetupError('OpenAI returned an unexpected model response. Existing settings were preserved.')
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code==401:
            raise SetupError('OpenAI rejected the key. Recopy an active API secret key from your project.') from None
        if code in (403,404):
            raise SetupError('This API project cannot currently access the selected realtime model. Check its permissions and model access.') from None
        raise SetupError('OpenAI model verification did not complete. Existing settings were preserved.') from None
    except SetupError:
        raise
    except Exception:
        raise SetupError('The model check could not complete. Existing settings were preserved.') from None


def private(path, directory=False):
    s=path.lstat()
    if not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode)) or s.st_uid!=os.geteuid() or s.st_mode&0o077:
        raise SetupError('Private voice storage has unexpected ownership or permissions.')


def atomic(path, value):
    fd,tmp=tempfile.mkstemp(prefix='.openai-voice-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            out.write(value);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def save_key(key):
    validate_key(key)
    private(STATE,directory=True)
    env=STATE/'gateway.env'
    target=STATE/'openai.json'
    for p in [env,target]:
        if p.exists() or p.is_symlink():private(p)
    lines=env.read_text().splitlines() if env.exists() else []
    lines=[line for line in lines if not line.startswith('OPENAI_API_KEY=')]
    # Key characters cannot inject a second environment assignment or shell syntax.
    updated='\n'.join(lines+[f'OPENAI_API_KEY={key}'])+'\n'
    atomic(target,json.dumps({'api_key':key,'model':MODEL},indent=2)+'\n')
    atomic(env,updated)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or not (BASE/'state/openclaw.json').is_file():
        raise SetupError('Use the root console on the assistant droplet, 203.0.113.10.')
    private(STATE,directory=True)
    if args.check:
        p=STATE/'openai.json';private(p);key=json.loads(p.read_text())['api_key']
    else:
        if not sys.stdin.isatty():raise SetupError('Use an interactive SSH or droplet console so the key stays hidden.')
        print('Connect OpenAI for realtime phone voice. This checks model access only; it does not start audio, enable calling or resize the server.')
        print('Paste the API secret key and press Enter. Hidden input shows no characters or dots.',flush=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                key=getpass.getpass('OpenAI API key (hidden): ').strip()
        except getpass.GetPassWarning:
            raise SetupError('This console cannot hide input safely. Use an interactive SSH terminal.') from None
    validate_key(key)
    print('Key format accepted. Checking access to '+MODEL+' (up to 25 seconds)...',flush=True)
    verify_key(key)
    if not args.check:save_key(key)
    print('OpenAI key and realtime model access verified.'+(' Credentials saved privately.' if not args.check else ' Saved settings unchanged.'))
    print('The existing phone mode remains active. Return to Codex and say: OpenAI voice key connected.')


if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):sys.exit('\nOpenAI voice setup cancelled; no calls placed.')
    except SetupError as e:sys.exit('OpenAI voice setup not completed. '+str(e))
    except Exception:sys.exit('OpenAI voice setup not completed. Check private storage and retry; no calls placed.')
