"""Read-only, resumable Yahoo Mail backup using only Python's standard library."""
import argparse
from contextlib import closing
import getpass
import hashlib
import imaplib
import json
import re
import sqlite3
import ssl
import sys
from pathlib import Path


def folders(connection):
    status, rows = connection.list()
    if status != 'OK':
        raise RuntimeError('Could not list mail folders')
    for row in rows:
        if not isinstance(row, bytes):
            raise RuntimeError('Unsupported folder listing')
        match = re.fullmatch(rb'\((.*?)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)', row)
        if not match:
            raise RuntimeError(f'Unsupported folder listing: {row!r}')
        if b'\\noselect' not in match[1].lower().split():
            yield match[2]  # Keep IMAP quoting and modified UTF-7 unchanged.


def save_message(connection, uid, path):
    status, data = connection.uid('fetch', uid, '(BODY.PEEK[])')
    if status != 'OK':
        raise RuntimeError(f'Could not download message UID {uid!r}')
    bodies = [item[1] for item in data if isinstance(item, tuple)]
    if len(bodies) != 1 or not isinstance(bodies[0], bytes):
        raise RuntimeError(f'Message UID {uid!r} returned no complete body')
    body = bodies[0]
    temporary = path.with_suffix('.part')
    temporary.write_bytes(body)
    temporary.replace(path)
    return len(body), hashlib.sha256(body).hexdigest()


def backup(connection, root, address, limit=None, inbox_only=False):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'manifest.sqlite3')) as database, database:
        database.execute('CREATE TABLE IF NOT EXISTS account (address TEXT PRIMARY KEY)')
        existing = database.execute('SELECT address FROM account').fetchone()
        if existing and existing[0] != address.casefold():
            raise RuntimeError('This output directory belongs to a different email account')
        database.execute('INSERT OR IGNORE INTO account VALUES (?)', (address.casefold(),))
        database.execute('CREATE TABLE IF NOT EXISTS messages (folder TEXT, validity TEXT, uid TEXT, size INTEGER, sha256 TEXT, PRIMARY KEY(folder, validity, uid))')
        database.commit()
        downloaded = skipped = 0
        for mailbox in folders(connection):
            if inbox_only and mailbox.strip(b'"').upper() != b'INBOX':
                continue
            label = mailbox.decode('ascii')
            status, _ = connection.select(mailbox, readonly=True)
            if status != 'OK':
                raise RuntimeError(f'Could not open folder {label}')
            _, values = connection.response('UIDVALIDITY')
            if not values or not values[0] or not values[0].isdigit():
                raise RuntimeError(f'Missing UIDVALIDITY for {label}')
            validity = values[0].decode('ascii')
            folder_id = hashlib.sha256(mailbox).hexdigest()
            directory = root / folder_id / validity
            directory.mkdir(parents=True, exist_ok=True)
            (directory.parent / 'folder.json').write_text(json.dumps({'imap_name': label}, indent=2), encoding='utf-8')
            status, data = connection.uid('search', None, 'ALL')
            if status != 'OK' or not data or not isinstance(data[0], bytes):
                raise RuntimeError(f'Could not list messages in {label}')
            uids = data[0].split()
            if any(not uid.isdigit() for uid in uids):
                raise RuntimeError('Server returned an invalid message UID')
            uids = sorted(uids, key=int, reverse=True)
            total = len(uids)
            if limit is not None:
                uids = uids[:limit]
            print(f'{label}: selected {len(uids)} of {total} messages', flush=True)
            for index, uid in enumerate(uids, 1):
                if not uid.isdigit():
                    raise RuntimeError('Server returned an invalid message UID')
                uid_text = uid.decode('ascii')
                path = directory / f'{uid_text}.eml'
                saved = database.execute('SELECT size, sha256 FROM messages WHERE folder=? AND validity=? AND uid=?', (label, validity, uid_text)).fetchone()
                if saved and path.is_file() and path.stat().st_size == saved[0] and hashlib.sha256(path.read_bytes()).hexdigest() == saved[1]:
                    skipped += 1
                    continue
                size, digest = save_message(connection, uid, path)
                database.execute('INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)', (label, validity, uid_text, size, digest))
                database.commit()
                downloaded += 1
                print(f'  {index}/{len(uids)} saved ({size:,} bytes)', flush=True)
        print(f'{"Selected backup" if limit is not None or inbox_only else "Backup"} complete: {downloaded} downloaded, {skipped} verified and skipped.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', help='Full Yahoo email address (prompted if omitted)')
    parser.add_argument('--output', type=Path, default=Path('yahoo-backup'), help='Backup directory (default: yahoo-backup)')
    parser.add_argument('--limit', type=int, help='Select the newest N messages per folder by IMAP UID')
    parser.add_argument('--inbox-only', action='store_true', help='Back up only the Inbox')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be at least 1')
    address = (args.email or input('Yahoo email address: ')).strip()
    if not address or '@' not in address:
        parser.error('Enter a full email address')
    password = getpass.getpass('Yahoo app password (hidden): ').replace(' ', '')
    connection = None
    try:
        connection = imaplib.IMAP4_SSL('imap.mail.yahoo.com', 993, ssl_context=ssl.create_default_context(), timeout=60)
        connection.login(address, password)
        backup(connection, args.output.resolve(), address, args.limit, args.inbox_only)
        return 0
    except (OSError, imaplib.IMAP4.error, RuntimeError, sqlite3.Error) as error:
        print(f'Backup stopped: {error}. Fix the problem and run the same command to resume.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped. Run the same command to resume.', file=sys.stderr)
        return 130
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


if __name__ == '__main__':
    sys.exit(main())

