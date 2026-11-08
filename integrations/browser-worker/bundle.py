#!/usr/bin/env python3
"""Build dist/install-procurator-browser-wsl.sh: the one-file Windows (WSL) installer for the home browser, with
browser_worker.py and the Node driver (web-plugin/runner.cjs) embedded. Run after changing either:
  python3 integrations/browser-worker/bundle.py"""
import base64, os, re, textwrap, time

HERE = os.path.dirname(os.path.abspath(__file__))
worker = open(os.path.join(HERE, 'browser_worker.py'), 'rb').read()
runner = open(os.path.join(HERE, '..', 'web-plugin', 'runner.cjs'), 'rb').read()
version = re.search(rb"VERSION = '([^']+)'", worker).group(1).decode()
wrap = lambda b: '\n'.join(textwrap.wrap(base64.b64encode(b).decode(), 76))
out = (open(os.path.join(HERE, 'install-wsl.sh.in')).read().replace('__WORKER_B64__', wrap(worker)).replace('__RUNNER_B64__', wrap(runner))
       .replace('__VERSION__', version).replace('__DATE__', time.strftime('%Y-%m-%d')))
os.makedirs(os.path.join(HERE, 'dist'), exist_ok=True)
path = os.path.join(HERE, 'dist', 'install-procurator-browser-wsl.sh')
with open(path, 'w', newline='\n') as f:
    f.write(out)
os.chmod(path, 0o755)
print('wrote', path, '(browser worker', version + ')')
