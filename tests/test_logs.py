import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import main


class LogTests(unittest.TestCase):
    def test_logs_append_and_errors_are_separate(self):
        logger = logging.getLogger('mail_backup_test')
        logger.propagate = False
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory, patch.object(main, 'LOGGER', logger), patch('builtins.print'):
            root = Path(directory)
            try:
                main.setup_logs(root)
                main.report('Downloaded message')
                main.report('Subject: Test; Date: today; retrying', level=logging.WARNING)
                main.setup_logs(root)
                main.report('Backup stopped', level=logging.ERROR)
                general = (root / 'logs.txt').read_text(encoding='utf-8')
                errors = (root / 'error logs.txt').read_text(encoding='utf-8')
                self.assertIn('INFO Downloaded message', general)
                self.assertNotIn('Downloaded message', errors)
                self.assertIn('WARNING Subject: Test; Date: today; retrying', errors)
                self.assertEqual(general.count('Backup stopped'), 1)
                self.assertIn('ERROR Backup stopped', errors)
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
