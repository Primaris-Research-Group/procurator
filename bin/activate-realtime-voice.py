#!/usr/bin/env python3
"""Apply the prepared realtime voice migration only after private key setup.

This never initiates a phone call. A new native Telegram review is required for
all subsequent calls. Default mode checks readiness without making changes.
"""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

BASE=Path('/opt/procurator')
STATE=Path('/var/lib/procurator-voice')
CONTAINER='procurator-openclaw-gateway-1'
BUNDLE=STATE/'realtime-candidate-v030.tgz'


class AudioPreflightError(RuntimeError):
    pass


def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)


def verify_audio(credentials):
    script=Path(__file__).with_name('check-openai-voice-session.mjs').read_text()
    try:
        result=subprocess.run(
            ['docker','exec','-i',CONTAINER,'node','--input-type=module','-e',script],
            input=json.dumps(credentials),text=True,capture_output=True,timeout=45)
        receipt=json.loads(result.stdout)
    except Exception:
        raise AudioPreflightError('OpenAI audio preflight could not complete. Phone settings unchanged.') from None
    if result.returncode or receipt.get('passed') is not True or receipt.get('audioBytes',0)<=0:
        if receipt.get('error')=='credit_balance_exhausted':
            raise AudioPreflightError('OpenAI API credit balance exhausted. Add credits to the API organization before retrying. Phone settings unchanged.')
        raise AudioPreflightError('OpenAI audio preflight failed. Check API billing, model access and connectivity. Phone settings unchanged.')
    print('Synthetic OpenAI realtime audio verified. No phone call placed.')


def atomic(path,value,uid=1000,gid=1000):
    tmp=path.with_name(path.name+'.realtime-stage')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(json.dumps(value,indent=2)+'\n');f.flush();os.fsync(f.fileno())
        os.chown(tmp,uid,gid);os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or not (BASE/'state/openclaw.json').is_file():
        raise RuntimeError('Use the assistant droplet root console.')
    saved=STATE/'openai.json'
    if not saved.is_file():
        raise RuntimeError('Run connect-openai-voice.py privately first. No mode change made.')
    info=saved.stat()
    if info.st_uid!=0 or info.st_mode&0o077 or saved.is_symlink():
        raise RuntimeError('OpenAI key storage is not private. No change made.')
    credentials=json.loads(saved.read_text())
    if credentials.get('model')!='gpt-realtime-2.1' or not credentials.get('api_key','').startswith('sk-'):
        raise RuntimeError('Unexpected private voice connection.')
    env=(STATE/'gateway.env').read_text().splitlines()
    if 'OPENAI_API_KEY='+credentials['api_key'] not in env:
        raise RuntimeError('Private voice environment does not match the saved connection.')
    if not BUNDLE.is_file():raise RuntimeError('The tested migration bundle is not staged.')
    current=json.loads((BASE/'state/openclaw.json').read_text())
    if current['plugins']['entries']['voice-call']['config'].get('realtime',{}).get('enabled'):
        print('Realtime voice is already enabled. No call placed or deployment repeated.');return
    print('Private key and staged migration are present. No call will be placed.')
    if not args.apply:return
    run('python3',str(BASE/'bin/connect-openai-voice.py'),'--check')
    # Model visibility alone does not establish funding or session acceptance.
    # Check real audio before stopping or changing the working gateway.
    verify_audio(credentials)
    probe='''import {callGatewayFromCli} from '/app/dist/plugin-sdk/gateway-runtime.js';
const r=await callGatewayFromCli('voicecall.status',{json:true,timeout:'15000'},{},{progress:false});
if(!Array.isArray(r.calls)||r.calls.length)throw new Error('A call is active or unconfirmed.');
console.log('No active calls.');'''
    run('docker','exec','-i',CONTAINER,'node','--input-type=module',input=probe,text=True)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=STATE/('before-realtime-'+stamp);backup.mkdir(mode=0o700)
    files=['state/openclaw.json','state/procurator-voice/settings.json','oauth-public/nginx.conf']
    for name in files:
        target=backup/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(BASE/name,target)
    for name in ['integrations/voice-plugin','integrations/voice-http','oauth-public/site/assistant']:
        shutil.copytree(BASE/name,backup/name)
    run('docker','stop',CONTAINER)
    try:
        with tarfile.open(BUNDLE) as t:
            if any(m.name.startswith('/') or '..' in Path(m.name).parts for m in t.getmembers()):
                raise RuntimeError('Unexpected migration bundle paths.')
            t.extractall(BASE,filter='data')
        spec=importlib.util.spec_from_file_location('realtime_stage',BASE/'bin/stage-realtime-voice.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        config=module.build_config(current)
        voice=config['plugins']['entries']['voice-call']['config']
        if voice.get('publicUrl')!='https://assistant.example.com/assistant/voice/webhook' or voice['serve']['path']!='/assistant/voice/webhook':
            raise RuntimeError('Unexpected public voice path; no route activation.')
        atomic(BASE/'state/openclaw.json',config)
        settings_path=BASE/'state/procurator-voice/settings.json';settings=json.loads(settings_path.read_text())
        settings['voiceMode']='openai-realtime';atomic(settings_path,settings)
        journal_path=BASE/'state/procurator-voice/tests.json';journal=json.loads(journal_path.read_text())
        if any(r['status'] in ('attempting','uncertain','initiated','hangup_requested') for r in journal['records']):
            raise RuntimeError('An unresolved call must be reconciled before migration.')
        for r in journal['records']:
            if r['status']=='pending':r['status']='cancelled';r['cancelReason']='Voice processing changed; a fresh review is required.'
        atomic(journal_path,journal)
        nginx_path=BASE/'oauth-public/nginx.conf';nginx=nginx_path.read_text()
        include='    include /opt/procurator/oauth-public/voice-realtime-location.conf;\n'
        marker='    location = /assistant/voice/webhook {'
        if marker not in nginx:raise RuntimeError('Expected TLS routing block not found.')
        if include not in nginx:nginx=nginx.replace(marker,include+marker)
        nginx_path.write_text(nginx)
        run('/usr/sbin/nginx','-t','-c',str(nginx_path))
        for name in ['index.html','privacy/index.html','terms/index.html']:
            shutil.copy2(BASE/'oauth-public/dist'/name,BASE/'oauth-public/site/assistant'/name)
        run('systemctl','restart','procurator-voice-http.service')
        run('docker','compose','-f','compose.yaml','up','-d','--no-deps','--force-recreate','openclaw-gateway',cwd=BASE)
        run('systemctl','reload','assistant-public-site.service')
        print('Realtime configuration applied. Wait for gateway health and verify the authenticated stream route before requesting a new owner-approved rehearsal. No call placed.')
        print('Private rollback backup:',backup)
    except Exception:
        for name in files:shutil.copy2(backup/name,BASE/name)
        for name in ['integrations/voice-plugin','integrations/voice-http','oauth-public/site/assistant']:
            shutil.copytree(backup/name,BASE/name,dirs_exist_ok=True)
        run('systemctl','restart','procurator-voice-http.service')
        run('docker','compose','-f','compose.yaml','up','-d','--no-deps','--force-recreate','openclaw-gateway',cwd=BASE)
        run('systemctl','reload','assistant-public-site.service')
        raise


if __name__=='__main__':
    try:main()
    except AudioPreflightError as e:
        raise SystemExit(str(e)) from None
    except Exception as e:
        # Do not print provider responses, credential values or raw config errors.
        raise SystemExit('Realtime activation did not complete. Check the private setup and deployment logs; no calls were initiated.') from None
