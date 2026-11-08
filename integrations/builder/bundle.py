#!/usr/bin/env python3
"""Build dist/install-procurator-builder-wsl.sh: the one-file Windows (WSL) installer with worker.py embedded.
Run after changing worker.py or install-wsl.sh.in:  python3 integrations/builder/bundle.py"""
import base64, os, re, textwrap, time

HERE = os.path.dirname(os.path.abspath(__file__))
worker = open(os.path.join(HERE, 'worker.py'), 'rb').read()
version = re.search(rb"VERSION = '([^']+)'", worker).group(1).decode()
template = open(os.path.join(HERE, 'install-wsl.sh.in')).read()
b64 = '\n'.join(textwrap.wrap(base64.b64encode(worker).decode(), 76))
out = template.replace('__WORKER_B64__', b64).replace('__VERSION__', version).replace('__DATE__', time.strftime('%Y-%m-%d'))
os.makedirs(os.path.join(HERE, 'dist'), exist_ok=True)
path = os.path.join(HERE, 'dist', 'install-procurator-builder-wsl.sh')
with open(path, 'w', newline='\n') as f:
    f.write(out)
os.chmod(path, 0o755)
print('wrote', path, '(worker', version + ')')
