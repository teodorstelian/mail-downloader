import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from main import backup, folders


class BackupTests(unittest.TestCase):
    def client(self):
        client = Mock()
        client.list.return_value = ('OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Noselect) "/" "Parent"'])
        client.select.return_value = ('OK', [b'1'])
        client.response.return_value = ('UIDVALIDITY', [b'123'])
        client.uid.side_effect = lambda command, *args: ('OK', [b'7']) if command == 'search' else ('OK', [(b'1 (BODY[] {9}', b'Subject: test\r\n\r\nmessage'), b')'])
        return client

    def test_resume_and_corruption_repair(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            client = self.client()
            backup(client, root, 'dad@yahoo.com')
            saved = next(root.rglob('*.eml'))
            original = saved.read_bytes()
            client.uid.reset_mock()
            backup(client, root, 'dad@yahoo.com')
            self.assertEqual([call.args[0] for call in client.uid.call_args_list], ['search'])
            saved.write_bytes(b'x' * len(original))
            backup(client, root, 'dad@yahoo.com')
            self.assertEqual(saved.read_bytes(), original)
            client.select.assert_called_with(b'"INBOX"', readonly=True)
            self.assertIn('BODY.PEEK[]', str(client.uid.call_args_list))
            with self.assertRaisesRegex(RuntimeError, 'different email account'):
                backup(client, root, 'other@yahoo.com')

    def test_failed_fetch_is_not_recorded(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            client = self.client()
            client.uid.side_effect = [('OK', [b'7']), ('OK', [None]), ('NO', [b'Unavailable'])]
            with self.assertRaisesRegex(RuntimeError, 'no complete body'):
                backup(client, Path(directory), 'dad@yahoo.com')
            self.assertEqual(list(Path(directory).rglob('*.eml')), [])

    def test_limit_selects_newest_uids_and_only_inbox(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            client = self.client()
            client.list.return_value[1].append(b'(\\HasNoChildren) "/" "Sent"')
            client.uid.side_effect = lambda command, *args: ('OK', [b'2 30 9']) if command == 'search' else ('OK', [(b'body', b'Subject: test\r\n\r\nmessage')])
            backup(client, Path(directory), 'dad@yahoo.com', limit=2, inbox_only=True)
            fetched = [call.args[1] for call in client.uid.call_args_list if call.args[0] == 'fetch']
            self.assertEqual(fetched, [b'30', b'9'])
            self.assertEqual(client.select.call_count, 1)
            client.uid.reset_mock()
            backup(client, Path(directory), 'dad@yahoo.com', limit=2, inbox_only=True)
            self.assertEqual([call.args[0] for call in client.uid.call_args_list], ['search'])

    def test_folder_quotes_preserved(self):
        client = Mock()
        client.list.return_value = ('OK', [b'(\\HasNoChildren) "/" "My \\"folder\\""'])
        self.assertEqual(list(folders(client)), [b'"My \\"folder\\""'])


    def test_received_period_and_total_count(self):
        from datetime import date
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            client = self.client()
            client.uid.side_effect = [('OK', [b'1 2 3 4']), ('OK', [b'2 3']), ('OK', [(b'body', b'message')])]
            output = StringIO()
            with redirect_stdout(output):
                backup(client, Path(directory), 'dad@yahoo.com', limit=1, start_date=date(2025, 9, 1), end_date=date(2025, 9, 30))
            self.assertIn('4 total messages', output.getvalue())
            self.assertIn('2 match the period; 1 selected', output.getvalue())
            self.assertEqual(client.uid.call_args_list[1].args, ('search', None, 'SINCE', '01-Sep-2025', 'BEFORE', '01-Oct-2025'))
            self.assertEqual(client.uid.call_args_list[2].args[1], b'3')


    def test_readable_folders_and_legacy_reuse(self):
        import hashlib
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            client = self.client()
            backup(client, root, 'dad@yahoo.com')
            saved = root / 'Inbox' / '123-7.eml'
            self.assertTrue(saved.is_file())
            legacy = root / hashlib.sha256(b'"INBOX"').hexdigest() / '123'
            legacy.mkdir(parents=True)
            saved.rename(legacy / '7.eml')
            client.uid.reset_mock()
            backup(client, root, 'dad@yahoo.com')
            self.assertTrue(saved.is_file())
            self.assertEqual([call.args[0] for call in client.uid.call_args_list], ['search'])

    def test_safe_names_and_collisions(self):
        from main import export_folder
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            first = export_folder(root, b'"Work/Invoices"')
            second = export_folder(root, b'"Work:Invoices"')
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, 'Work_Invoices')
            self.assertEqual(export_folder(root, b'"Work/Invoices"'), first)
            for mailbox in (b'"../outside"', b'"CON"', b'".."'):
                path = export_folder(root, mailbox)
                self.assertEqual(path.resolve().parent, root.resolve())

    def test_fetch_no_reports_reason_and_can_resume(self):
        from main import MessageDownloadError
        import sqlite3
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            client = self.client()
            client.uid.side_effect = [('OK', [b'55005']), ('NO', [b'[UNAVAILABLE] Try again later']), ('OK', [(b'headers', b'Subject: Important message\r\nDate: Tue, 20 Oct 2026 10:00:00 +0300\r\n\r\n')])]
            with self.assertRaisesRegex(MessageDownloadError, '55005.*FETCH NO.*UNAVAILABLE.*Subject: Important message.*Date: Tue, 20 Oct 2026'):
                backup(client, root, 'dad@yahoo.com')
            from contextlib import closing
            with closing(sqlite3.connect(root / 'manifest.sqlite3')) as database:
                self.assertEqual(database.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(list(root.rglob('*.eml')), [])
            client.uid.side_effect = [('OK', [b'55005']), ('OK', [(b'body', b'complete message')])]
            backup(client, root, 'dad@yahoo.com')
            self.assertEqual((root / 'Inbox' / '123-55005.eml').read_bytes(), b'complete message')

    def test_error_headers_decode_subject_and_handle_lookup_failure(self):
        from main import save_message, MessageDownloadError
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / 'message.eml'
            client = Mock()
            client.uid.side_effect = [('NO', [b'Try later']), ('OK', [(b'headers', b'Subject: =?utf-8?b?Q2Fmw6k=?=\r\nDate: Tue, 20 Oct 2026 10:00:00 +0300\r\n\r\n')])]
            with self.assertRaisesRegex(MessageDownloadError, 'Subject: Café; Date: Tue, 20 Oct 2026'):
                save_message(client, b'55005', path)
            self.assertEqual(client.uid.call_args.args[2], '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])')
            self.assertFalse(path.exists())
            client.uid.side_effect = [('NO', [b'Try later']), TimeoutError('timeout')]
            with self.assertRaisesRegex(MessageDownloadError, '55005.*Subject: unavailable.*header lookup failed'):
                save_message(client, b'55005', path)

if __name__ == '__main__':
    unittest.main()

