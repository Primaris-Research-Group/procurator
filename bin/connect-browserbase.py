#!/usr/bin/env python3
"""Enter or check the Browserbase key privately on the deployed host."""
import argparse
import getpass
import json
import os
from pathlib import Path
import urllib.request

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    path=Path('/var/lib/procurator-web/browserbase.env')
    if args.check:key=path.read_text().strip().split('=',1)[1]
    else:key=getpass.getpass('Browserbase API key (hidden): ').strip()
    if not key.startswith('bb_') or '\n' in key:raise SystemExit('Use a valid Browserbase API key.')
    request=urllib.request.Request('https://api.browserbase.com/v1/projects',headers={'X-BB-API-Key':key})
    try:
        with urllib.request.urlopen(request,timeout=25) as response:projects=json.load(response)
        if not isinstance(projects,list) or not projects:raise ValueError()
    except Exception:raise SystemExit('Browserbase access was not confirmed. Provider details and credentials were withheld.') from None
    if not args.check:
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);path.parent.chmod(0o700)
        temp=path.with_suffix('.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:f.write('BROWSERBASE_API_KEY='+key+'\n')
        os.replace(temp,path);path.chmod(0o600)
    print('Browserbase account access verified. Credential value withheld. No project-ID setup is needed.')
if __name__=='__main__':main()
