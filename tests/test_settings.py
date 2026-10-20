import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import main


class SettingsTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, 'setup_logs')
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_literal_paths_quotes_and_environment_precedence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / '.env'
            path.write_text('# comment\nYAHOO_EMAIL=you@yahoo.com\nYAHOO_APP_PASSWORD="fake=secret#literal"\nBACKUP_OUTPUT="D:\\Mail Backup"\n', encoding='utf-8')
            with patch.dict(os.environ, {}, clear=True):
                settings = main.load_settings(path)
                self.assertEqual(settings['BACKUP_OUTPUT'], 'D:\\Mail Backup')
                self.assertEqual(settings['YAHOO_APP_PASSWORD'], 'fake=secret#literal')
            with patch.dict(os.environ, {'YAHOO_EMAIL': 'override@yahoo.com'}, clear=True):
                self.assertEqual(main.load_settings(path)['YAHOO_EMAIL'], 'override@yahoo.com')
            path.write_text('not-a-setting secret', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'line 1') as error:
                main.load_settings(path)
            self.assertNotIn('secret', str(error.exception))

    def test_short_command_uses_settings_and_cli_overrides(self):
        settings = {'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake password', 'BACKUP_OUTPUT': 'backups'}
        connection = Mock()
        with patch.object(main, 'load_settings', return_value=settings), patch.object(main.imaplib, 'IMAP4_SSL', return_value=connection), patch.object(main, 'backup') as backup, patch.object(main.getpass, 'getpass') as prompt, patch('sys.argv', ['main.py', '--inbox-only', '--limit', '10', '--output', 'override']):
            self.assertEqual(main.main(), 0)
            connection.login.assert_called_once_with('you@yahoo.com', 'fakepassword')
            prompt.assert_not_called()
            backup.assert_called_once_with(connection, Path('override').resolve(), 'you@yahoo.com', 10, True, start_date=None, end_date=None)
            connection.logout.assert_called_once()

    def test_blank_password_prompts(self):
        with patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': ''}), patch.object(main.imaplib, 'IMAP4_SSL') as client, patch.object(main, 'backup'), patch.object(main.getpass, 'getpass', return_value='fake') as prompt, patch('sys.argv', ['main.py']):
            self.assertEqual(main.main(), 0)
            prompt.assert_called_once()
            client.return_value.login.assert_called_once_with('you@yahoo.com', 'fake')

    def test_env_limit_and_command_override(self):
        for configured, arguments, expected in [('10', [], 10), ('', [], None), ('10', ['--limit', '2'], 2)]:
            with self.subTest(configured=configured, arguments=arguments), patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake', 'BACKUP_LIMIT': configured}), patch.object(main.imaplib, 'IMAP4_SSL'), patch.object(main, 'backup') as backup, patch('sys.argv', ['main.py'] + arguments):
                self.assertEqual(main.main(), 0)
                self.assertEqual(backup.call_args.args[3], expected)

    def test_invalid_env_limits_fail_before_login(self):
        for value in ['0', '-1', 'invalid']:
            with self.subTest(value=value), patch.object(main, 'load_settings', return_value={'BACKUP_LIMIT': value}), patch.object(main.imaplib, 'IMAP4_SSL') as client, patch('sys.argv', ['main.py']), patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    main.main()
                client.assert_not_called()

    def test_date_validation_and_defaults(self):
        from datetime import date
        self.assertEqual(main.parse_date('2024-02-29'), date(2024, 2, 29))
        for value in ['2025-02-29', '2025-9-01', 'bad']:
            with self.assertRaises(main.argparse.ArgumentTypeError):
                main.parse_date(value)
        with patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake', 'BACKUP_START_DATE': '2025-09-01', 'BACKUP_END_DATE': '2025-09-30'}), patch.object(main.imaplib, 'IMAP4_SSL'), patch.object(main, 'backup') as backup, patch('sys.argv', ['main.py']):
            self.assertEqual(main.main(), 0)
            self.assertEqual(backup.call_args.kwargs, {'start_date': date(2025, 9, 1), 'end_date': date(2025, 9, 30)})
        with patch.object(main, 'load_settings', return_value={}), patch('sys.argv', ['main.py', '--start-date', '2025-10-01', '--end-date', '2025-09-01']), patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                main.main()

    def test_folder_setting_and_override(self):
        for configured, arguments, expected in [('inbox', [], True), ('all', [], False), ('inbox', ['--folders', 'all'], False), ('all', ['--inbox-only'], True)]:
            with self.subTest(configured=configured, arguments=arguments), patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake', 'BACKUP_FOLDERS': configured}), patch.object(main.imaplib, 'IMAP4_SSL') as client, patch.object(main, 'backup') as backup, patch('sys.argv', ['main.py'] + arguments):
                self.assertEqual(main.main(), 0)
                self.assertEqual(backup.call_args.args[4], expected)
                self.assertEqual(client.call_args.args[0], 'export.imap.mail.yahoo.com')
        with patch.object(main, 'load_settings', return_value={'BACKUP_FOLDERS': 'typo'}), patch('sys.argv', ['main.py']), patch('sys.stderr'), patch.object(main.imaplib, 'IMAP4_SSL') as client:
            with self.assertRaises(SystemExit):
                main.main()
            client.assert_not_called()

    def test_timeout_reconnects_and_resumes(self):
        first, second = Mock(), Mock()
        with patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake', 'IMAP_TIMEOUT': '240', 'BACKUP_RETRIES': '1'}), patch.object(main.imaplib, 'IMAP4_SSL', side_effect=[first, second]) as client, patch.object(main, 'backup', side_effect=[TimeoutError('read timed out'), None]) as backup, patch.object(main.time, 'sleep') as sleep, patch('sys.argv', ['main.py']):
            self.assertEqual(main.main(), 0)
            self.assertEqual(backup.call_count, 2)
            self.assertEqual(client.call_args.kwargs['timeout'], 240)
            first.shutdown.assert_called_once()
            first.logout.assert_not_called()
            second.login.assert_called_once_with('you@yahoo.com', 'fake')
            second.logout.assert_called_once()
            sleep.assert_called_once_with(5)

    def test_retries_are_bounded_and_auth_errors_are_not_retried(self):
        for failure, expected in [(main.MessageDownloadError('UID 55005 FETCH NO'), 2), (TimeoutError('timeout'), 2), (main.imaplib.IMAP4.error('Authentication failed'), 1)]:
            with self.subTest(failure=type(failure)), patch.object(main, 'load_settings', return_value={'YAHOO_EMAIL': 'you@yahoo.com', 'YAHOO_APP_PASSWORD': 'fake', 'BACKUP_RETRIES': '1'}), patch.object(main.imaplib, 'IMAP4_SSL') as client, patch.object(main, 'backup'), patch.object(main.time, 'sleep'), patch('sys.argv', ['main.py']), patch('sys.stderr'):
                client.return_value.login.side_effect = failure
                self.assertEqual(main.main(), 1)
                self.assertEqual(client.call_count, expected)
