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
            client.uid.side_effect = [('OK', [b'7']), ('OK', [None])]
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


if __name__ == '__main__':
    unittest.main()

