"""Owner-scoped iCloud changes: read/unread flags and moves to Junk, Inbox or Deleted Messages.

iCloud IMAP has no MOVE/UIDPLUS, so a move is COPY (atomic per RFC 3501) + \\Deleted on the
source + EXPUNGE. EXPUNGE removes every \\Deleted message in the folder, so it runs only when
the \\Deleted set is exactly the messages this action moved; otherwise they are left flagged
for the owner's Mail app to finish, and nothing else is permanently removed.
"""
from email import policy
from email.parser import BytesParser
import re

from icloud_reader import ICloudReader, fetched_messages, uid as check_uid

MAX_TARGETS = 50
DESTINATIONS = {
    'trash': ('deleted messages', 'trash', 'deleted items'),
    'junk': ('junk', 'spam', 'bulk mail'),
    'not_junk': ('inbox',),
}


def uid_set(uids):
    if not isinstance(uids, list) or not 1 <= len(uids) <= MAX_TARGETS:
        raise ValueError(f'Select 1–{MAX_TARGETS} messages.')
    clean = sorted({check_uid(u) for u in uids}, key=int)
    return clean, ','.join(clean)


class ICloudManager(ICloudReader):
    def _open(self, client, folder_id, uidvalidity, readonly):
        folders = self.inventory(client)
        if folder_id not in folders:
            raise ValueError('That folder is no longer available; refresh the folder list.')
        folder = folders[folder_id]
        if client.select(folder['wire'], readonly=readonly)[0] != 'OK':
            raise RuntimeError('Could not open the iCloud folder.')
        values = client.response('UIDVALIDITY')[1]
        validity = values[0].decode('ascii') if values and values[0] else None
        if validity != check_uid(uidvalidity):
            raise ValueError('Mailbox UIDVALIDITY changed. Look the messages up again before changing them.')
        return folders, folder

    @staticmethod
    def _present(client, joined):
        ok, found = client.uid('search', 'UID', joined)
        if ok != 'OK':
            raise RuntimeError('iCloud did not confirm which messages exist.')
        return sorted({x.decode('ascii') for x in (found[0] or b'').split()}, key=int)

    def headers(self, folder_id, uidvalidity, uids):
        """Sender/subject/date for an approval review; does not mark anything read."""
        clean, joined = uid_set(uids)
        with self.connect() as client:
            _, folder = self._open(client, folder_id, uidvalidity, readonly=True)
            present = self._present(client, joined)
            rows = []
            if present:
                ok, chunks = client.uid('fetch', ','.join(present), '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]<0.2048>)')
                if ok != 'OK':
                    raise RuntimeError('iCloud message details could not be read.')
                fetched = fetched_messages(chunks, set(present))
                for u in present:
                    if u in fetched:
                        m = BytesParser(policy=policy.default).parsebytes(fetched[u][0])
                        rows.append({'uid': u, 'from': str(m.get('from', ''))[:120], 'subject': str(m.get('subject', ''))[:160], 'date': str(m.get('date', ''))[:40]})
        return {'folder_name': folder['name'], 'messages': rows, 'missing': [u for u in clean if u not in present]}

    def apply(self, folder_id, uidvalidity, uids, op):
        if op not in ('mark_read', 'mark_unread', 'trash', 'junk', 'not_junk'):
            raise ValueError('Unsupported iCloud change.')
        clean, joined = uid_set(uids)
        with self.connect() as client:
            folders, folder = self._open(client, folder_id, uidvalidity, readonly=False)
            present = self._present(client, joined)
            missing = [u for u in clean if u not in present]
            if not present:
                return {'changed': [], 'missing': missing, 'folder_name': folder['name']}
            targets = ','.join(present)
            if op in ('mark_read', 'mark_unread'):
                sign = '+FLAGS' if op == 'mark_read' else '-FLAGS'
                if client.uid('store', targets, sign, '(\\Seen)')[0] != 'OK':
                    raise RuntimeError('iCloud did not confirm the read/unread change.')
                return {'changed': present, 'missing': missing, 'folder_name': folder['name'], 'action': op}
            names = DESTINATIONS[op]
            dest = [v for k, v in folders.items() if (k == 'inbox' if op == 'not_junk' else v['name'].lower() in names)]
            if len(dest) != 1:
                raise RuntimeError('Could not identify exactly one iCloud destination folder. Nothing was changed.')
            dest = dest[0]
            if dest['wire'] == folder['wire']:
                raise ValueError('These messages are already in ' + dest['name'] + '.')
            if op == 'not_junk' and folder['name'].lower() not in DESTINATIONS['junk']:
                raise ValueError('"Not junk" moves messages out of the Junk folder only.')
            if client.uid('copy', targets, dest['wire'])[0] != 'OK':
                raise RuntimeError('iCloud did not confirm copying to ' + dest['name'] + '. Nothing was removed from the original folder.')
            if client.uid('store', targets, '+FLAGS', '(\\Deleted)')[0] != 'OK':
                return {'changed': present, 'missing': missing, 'folder_name': folder['name'], 'destination': dest['name'],
                        'action': op, 'original_removed': False,
                        'note': 'Copied to ' + dest['name'] + ', but iCloud did not confirm removing the originals; they may appear in both folders.'}
            ok, flagged = client.uid('search', 'DELETED')
            flagged = {x.decode('ascii') for x in (flagged[0] or b'').split()} if ok == 'OK' else None
            expunged = flagged == set(present)
            if expunged:
                client.expunge()
            return {'changed': present, 'missing': missing, 'folder_name': folder['name'], 'destination': dest['name'], 'action': op,
                    'original_removed': expunged,
                    **({} if expunged else {'note': 'Copied and marked deleted in ' + folder['name'] + '; other messages there were already marked deleted, so final removal was left to your Mail app to avoid removing anything else.'})}
