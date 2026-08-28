import unittest
from unittest.mock import patch, MagicMock
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT_DIR / "services" / "strategy-engine"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from utils.notifications import EmailNotifier


class TestEmailNotifier(unittest.TestCase):
    def setUp(self):
        self.env_patches = {
            "SMTP_HOST": "smtp.mock.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "user@mock.com",
            "SMTP_PASSWORD": "mock_password",
            "SMTP_USE_TLS": "true",
            "SMTP_FROM": "bot@mock.com",
            "SMTP_TO": "user@mock.com,other@mock.com",
        }
        for k, v in self.env_patches.items():
            os.environ[k] = v

    def tearDown(self):
        for k in self.env_patches.keys():
            os.environ.pop(k, None)

    def test_from_env_parsing(self):
        notifier = EmailNotifier.from_env()
        self.assertEqual(notifier.smtp_host, "smtp.mock.com")
        self.assertEqual(notifier.smtp_port, 587)
        self.assertEqual(notifier.smtp_username, "user@mock.com")
        self.assertEqual(notifier.smtp_password, "mock_password")
        self.assertTrue(notifier.smtp_use_tls)
        self.assertEqual(notifier.smtp_from, "bot@mock.com")
        self.assertEqual(notifier.smtp_to, "user@mock.com,other@mock.com")

    @patch("smtplib.SMTP")
    def test_send_email_success(self, mock_smtp_cls):
        mock_smtp_inst = MagicMock()
        mock_smtp_cls.return_value = mock_smtp_inst

        notifier = EmailNotifier.from_env()
        notifier.send("Test Subject", "Test Plaintext Body", "<h1>Test HTML</h1>")

        # Verify SMTP server was instantiated and methods called
        mock_smtp_cls.assert_called_once_with("smtp.mock.com", 587, timeout=15)
        mock_smtp_inst.starttls.assert_called_once()
        mock_smtp_inst.login.assert_called_once_with("user@mock.com", "mock_password")
        
        # Verify sendmail was called with correct recipients and sender
        mock_smtp_inst.sendmail.assert_called_once()
        args = mock_smtp_inst.sendmail.call_args[0]
        self.assertEqual(args[0], "bot@mock.com")
        self.assertEqual(args[1], ["user@mock.com", "other@mock.com"])
        
        # Verify message string contains subject and bodies
        msg_str = args[2]
        self.assertIn("Subject: Test Subject", msg_str)
        self.assertIn("Test Plaintext Body", msg_str)
        self.assertIn("<h1>Test HTML</h1>", msg_str)
        
        mock_smtp_inst.quit.assert_called_once()

    @patch("smtplib.SMTP")
    def test_send_suppressed_when_no_host(self, mock_smtp_cls):
        os.environ["SMTP_HOST"] = ""
        notifier = EmailNotifier.from_env()
        notifier.send("Subject", "Body")
        
        # SMTP should not be called
        mock_smtp_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
