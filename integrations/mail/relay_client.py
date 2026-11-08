"""One fixed SSH relay, configured privately by the operator; no model-selected destination."""
import base64
import hashlib
import ipaddress
import json
import subprocess


class RelayClient:
    def __init__(self, directory):
        self.directory = directory

    def icloud_file(self):
        """0.29.0: console file first, then the Accounts tab's copy in the web secrets folder."""
        import os
        legacy = self.directory / 'icloud.json'
        from pathlib import Path
        return legacy if legacy.exists() else Path(os.environ.get('PROCURATOR_SECRETS', '/var/lib/procurator-web/secrets')) / 'accounts' / 'icloud-mail.json'

    def configured(self):
        try:
            config = json.loads((self.directory / 'relay.json').read_text())
            ipaddress.ip_address(config['host'])
            return config.get('smtp_verified') is True and all((self.directory / f).is_file() for f in ('relay_ed25519','relay-known-hosts'))
        except (OSError, ValueError, KeyError):
            return False

    def call(self, action, identifier=None, raw=None):
        config = json.loads((self.directory / 'relay.json').read_text())
        host = str(ipaddress.ip_address(config['host']))
        request = {'action': action}
        if identifier:
            request['id'] = identifier
        if action in ('send', 'check'):
            request['password'] = json.loads(self.icloud_file().read_text())['password']
        if raw is not None:
            request.update(raw=base64.b64encode(raw).decode('ascii'), sha256=hashlib.sha256(raw).hexdigest())
        command = ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(self.directory / 'relay-known-hosts'),
                   '-o', 'LogLevel=ERROR', '-i', str(self.directory / 'relay_ed25519'), 'mailrelay@' + host]
        result = subprocess.run(command, input=(json.dumps(request)+'\n').encode(), capture_output=True, timeout=65,
                                env={'PATH':'/usr/bin:/bin','HOME':str(self.directory)})
        if result.returncode:
            raise RuntimeError('The iCloud relay did not confirm an outcome. Check status; never retry with a new draft.')
        response = json.loads(result.stdout)
        if response.get('ok') is not True:
            raise RuntimeError('The iCloud relay could not complete this request.')
        return response['result']
