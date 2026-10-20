"""Read-only, resumable Yahoo Mail backup using only Python's standard library."""
import argparse
from contextlib import closing
from datetime import date, timedelta
from email import policy
from email.parser import BytesHeaderParser
import getpass
import logging
import os
import hashlib
import imaplib
import json
import re
import sqlite3
import base64
import shutil
import ssl
import sys
import time
from pathlib import Path


LOGGER = logging.getLogger('mail_backup')
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def setup_logs(directory):
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    try:
        for filename, level in [('logs.txt', logging.INFO), ('error logs.txt', logging.WARNING)]:
            handler = logging.FileHandler(directory / filename, mode='a', encoding='utf-8')
            handler.setLevel(level)
            handler.setFormatter(formatter)
            LOGGER.addHandler(handler)
    except OSError:
        for handler in list(LOGGER.handlers):
            LOGGER.removeHandler(handler)
            handler.close()
        raise


def report(message, level=logging.INFO, **print_options):
    LOGGER.log(level, message)
    print(message, **print_options)


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


def export_folder(root, mailbox):
    """Map an IMAP name to a safe, readable Windows directory."""
    label = mailbox.decode('ascii')
    name = label
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r'\\(.)', r'\1', name[1:-1])

    def decode_utf7(match):
        value = match.group(1)
        if not value:
            return '&'
        try:
            raw = value.replace(',', '/')
            return base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True).decode('utf-16-be')
        except (ValueError, UnicodeError):
            return match.group(0)

    name = re.sub(r'&([^-]*)-', decode_utf7, name)
    if name.upper() == 'INBOX':
        name = 'Inbox'
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:100]
    if not name:
        name = 'Folder'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = '_' + name
    candidate = root / name
    suffix = hashlib.sha256(mailbox).hexdigest()
    for attempt in range(2):
        if candidate.resolve().parent != root.resolve():
            raise RuntimeError('Export folder resolves outside the backup directory')
        metadata = candidate / 'folder.json'
        if not candidate.exists():
            candidate.mkdir()
            metadata.write_text(json.dumps({'imap_name': label}, indent=2), encoding='utf-8')
            return candidate
        if metadata.is_file() and json.loads(metadata.read_text(encoding='utf-8')).get('imap_name') == label:
            return candidate
        candidate = root / f'{name}-{suffix}'
    raise RuntimeError('Cannot safely assign an export folder name')


class MessageDownloadError(RuntimeError):
    """A failed FETCH that can be retried using a fresh connection."""


def fetch_failure(uid, status, data):
    # Only expose standalone server status text, never message literals.
    responses = [item.decode('utf-8', errors='replace') for item in (data or []) if isinstance(item, bytes)]
    detail = ' '.join(responses)
    detail = re.sub(r'[\x00-\x1f\x7f]', ' ', detail)[:300]
    return f'Could not download message UID {uid.decode("ascii")} (FETCH {status})' + (f': {detail}' if detail else '')


def message_error_context(connection, uid):
    """Best-effort headers for an error; never mark the message as read."""
    try:
        status, data = connection.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])')
        if status != 'OK':
            return 'Subject: unavailable; Date: unavailable'
        bodies = [item[1] for item in (data or []) if isinstance(item, tuple) and isinstance(item[1], bytes)]
        if len(bodies) != 1:
            return 'Subject: unavailable; Date: unavailable'
        headers = BytesHeaderParser(policy=policy.default).parsebytes(bodies[0])

        def display(name):
            value = str(headers.get(name) or '(missing)')
            return re.sub(r'[\x00-\x1f\x7f]', ' ', value)[:300]

        return f'Subject: {display("Subject")}; Date: {display("Date")}'
    except (OSError, imaplib.IMAP4.error, ValueError):
        return 'Subject: unavailable; Date: unavailable (header lookup failed)'


def save_message(connection, uid, path):
    status, data = connection.uid('fetch', uid, '(BODY.PEEK[])')
    if status != 'OK':
        error = fetch_failure(uid, status, data) + '; ' + message_error_context(connection, uid)
        if status == 'NO':
            raise MessageDownloadError(error)
        raise RuntimeError(error)
    bodies = [item[1] for item in data if isinstance(item, tuple)]
    if len(bodies) != 1 or not isinstance(bodies[0], bytes):
        raise MessageDownloadError(f'Message UID {uid.decode("ascii")} returned no complete body; ' + message_error_context(connection, uid))
    body = bodies[0]
    temporary = path.with_suffix('.part')
    temporary.write_bytes(body)
    temporary.replace(path)
    return len(body), hashlib.sha256(body).hexdigest()


def backup(connection, root, address, limit=None, inbox_only=False, start_date=None, end_date=None):
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
            legacy_directory = root / hashlib.sha256(mailbox).hexdigest() / validity
            directory = export_folder(root, mailbox)
            status, data = connection.uid('search', None, 'ALL')
            if status != 'OK' or not data or not isinstance(data[0], bytes):
                raise RuntimeError(f'Could not list messages in {label}')
            total = len(data[0].split())
            report(f'{label}: {total:,} total messages visible to the server', flush=True)
            if total == 10000:
                report('  Note: exactly 10,000 messages are visible. If Yahoo webmail shows more, this backup may be incomplete.', level=logging.WARNING, flush=True)
            if start_date or end_date:
                criteria = []
                if start_date:
                    criteria.extend(['SINCE', imap_date(start_date)])
                if end_date:
                    criteria.extend(['BEFORE', imap_date(end_date + timedelta(days=1))])
                status, data = connection.uid('search', None, *criteria)
                if status != 'OK' or not data or not isinstance(data[0], bytes):
                    raise RuntimeError(f'Could not search dates in {label}')
            uids = data[0].split()
            if any(not uid.isdigit() for uid in uids):
                raise RuntimeError('Server returned an invalid message UID')
            uids = sorted(uids, key=int, reverse=True)
            matching = len(uids)
            if limit is not None:
                uids = uids[:limit]
            report(f'{label}: {matching:,} match the period; {len(uids):,} selected after limit', flush=True)
            for index, uid in enumerate(uids, 1):
                if not uid.isdigit():
                    raise RuntimeError('Server returned an invalid message UID')
                uid_text = uid.decode('ascii')
                path = directory / f'{validity}-{uid_text}.eml'
                saved = database.execute('SELECT size, sha256 FROM messages WHERE folder=? AND validity=? AND uid=?', (label, validity, uid_text)).fetchone()
                legacy = legacy_directory / f'{uid_text}.eml'
                if saved and not path.exists() and legacy.is_file() and legacy.stat().st_size == saved[0] and hashlib.sha256(legacy.read_bytes()).hexdigest() == saved[1]:
                    temporary = path.with_suffix('.part')
                    shutil.copyfile(legacy, temporary)
                    temporary.replace(path)
                if saved and path.is_file() and path.stat().st_size == saved[0] and hashlib.sha256(path.read_bytes()).hexdigest() == saved[1]:
                    skipped += 1
                    continue
                try:
                    size, digest = save_message(connection, uid, path)
                except MessageDownloadError as error:
                    raise MessageDownloadError(f'Folder {label}: {error}') from error
                database.execute('INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)', (label, validity, uid_text, size, digest))
                database.commit()
                downloaded += 1
                report(f'  {index}/{len(uids)} saved ({size:,} bytes)', flush=True)
        report(f'{"Selected backup" if limit is not None or inbox_only or start_date or end_date else "Backup"} complete: {downloaded} downloaded, {skipped} verified and skipped.')


def load_settings(path):
    """Read simple KEY=value settings without executing or expanding their contents."""
    settings = {}
    if path.exists():
        for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key):
                raise ValueError(f'Invalid .env setting on line {number}; expected KEY=value')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f'Unclosed quote in .env on line {number}')
                value = value[1:-1]
            settings[key] = value
    for key in ('YAHOO_EMAIL', 'YAHOO_APP_PASSWORD', 'BACKUP_OUTPUT', 'BACKUP_LIMIT', 'BACKUP_START_DATE', 'BACKUP_END_DATE', 'BACKUP_FOLDERS', 'IMAP_TIMEOUT', 'BACKUP_RETRIES'):
        if key in os.environ:
            settings[key] = os.environ[key]
    return settings


def parse_date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        result = date.fromisoformat(value)
        if result == date.max:
            raise ValueError
        return result
    except ValueError:
        raise argparse.ArgumentTypeError('Use a valid YYYY-MM-DD date before 9999-12-31') from None


def imap_date(value):
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    return f'{value.day:02d}-{months[value.month - 1]}-{value.year:04d}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    try:
        settings = load_settings(Path(__file__).resolve().parent / '.env')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    parser.add_argument('--email', default=settings.get('YAHOO_EMAIL'), help='Override email from .env')
    parser.add_argument('--output', type=Path, default=Path(settings.get('BACKUP_OUTPUT') or 'yahoo-backup'), help='Override backup directory from .env')
    parser.add_argument('--limit', type=int, default=settings.get('BACKUP_LIMIT') or None, help='Override BACKUP_LIMIT; newest N messages per folder by IMAP UID')
    folder_options = parser.add_mutually_exclusive_group()
    folder_options.add_argument('--folders', choices=('inbox', 'all'), default=settings.get('BACKUP_FOLDERS') or 'all', help='Override BACKUP_FOLDERS (inbox or all)')
    folder_options.add_argument('--inbox-only', action='store_true', help='Back up only the Inbox')
    parser.add_argument('--start-date', type=parse_date, default=settings.get('BACKUP_START_DATE') or None, help='Include mail received on/after YYYY-MM-DD')
    parser.add_argument('--end-date', type=parse_date, default=settings.get('BACKUP_END_DATE') or None, help='Include mail received on/before YYYY-MM-DD')
    parser.add_argument('--timeout', type=int, default=settings.get('IMAP_TIMEOUT') or 300, help='Seconds to wait for a network operation (default: 300)')
    parser.add_argument('--retries', type=int, default=settings.get('BACKUP_RETRIES') or 5, help='Reconnect attempts after network failures (default: 5)')
    args = parser.parse_args()
    if args.timeout < 1 or args.retries < 0:
        parser.error('Timeout must be positive and retries must be zero or greater')
    if args.folders not in ('inbox', 'all'):
        parser.error('BACKUP_FOLDERS must be inbox or all')
    inbox_only = args.inbox_only or args.folders == 'inbox'
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error('Start date must be on or before end date')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be at least 1')
    address = (args.email or input('Yahoo email address: ')).strip()
    if not address or '@' not in address:
        parser.error('Enter a full email address')
    try:
        setup_logs(Path(__file__).resolve().parent)
    except OSError as error:
        print(f'Could not open backup logs: {error}', file=sys.stderr)
        return 1
    LOGGER.info('Backup run started; output=%s; folders=%s; limit=%s; start=%s; end=%s', args.output.resolve(), 'inbox' if inbox_only else 'all', args.limit, args.start_date, args.end_date)
    connection = None
    try:
        password = (settings.get('YAHOO_APP_PASSWORD') or getpass.getpass('Yahoo app password (hidden): ')).replace(' ', '')
        if not password:
            raise RuntimeError('A Yahoo app password is required')
        for attempt in range(args.retries + 1):
            try:
                connection = imaplib.IMAP4_SSL('export.imap.mail.yahoo.com', 993, ssl_context=ssl.create_default_context(), timeout=args.timeout)
                connection.login(address, password)
                backup(connection, args.output.resolve(), address, args.limit, inbox_only, start_date=args.start_date, end_date=args.end_date)
                break
            except (TimeoutError, ConnectionError, imaplib.IMAP4.abort, MessageDownloadError) as error:
                if connection is not None:
                    try:
                        connection.shutdown()
                    except (OSError, imaplib.IMAP4.error):
                        pass
                    connection = None
                if attempt == args.retries:
                    raise
                delay = min(5 * (2 ** attempt), 30)
                report(f'Backup interrupted: {error}. Reconnecting in {delay}s (retry {attempt + 1}/{args.retries}); verified files will be skipped.', level=logging.WARNING, flush=True)
                time.sleep(delay)
        return 0
    except (OSError, imaplib.IMAP4.error, RuntimeError, sqlite3.Error, ValueError) as error:
        report(f'Backup stopped: {error}. Fix the problem and run the same command to resume.', level=logging.ERROR, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nStopped. Run the same command to resume.', level=logging.ERROR, file=sys.stderr)
        return 130
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


if __name__ == '__main__':
    sys.exit(main())

