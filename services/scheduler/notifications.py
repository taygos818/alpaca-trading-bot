import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

EMAIL_LOGGER = logging.getLogger("email-notifier")


class DiscordNotifier:
    def __init__(self, webhook_url: str = ""):
        self.webhook_url = webhook_url

    @classmethod
    def from_env(cls):
        return cls(os.getenv("DISCORD_WEBHOOK_URL", ""))

    def send(self, message: str):
        if not self.webhook_url:
            return
        response = requests.post(self.webhook_url, json={"content": message}, timeout=10)
        response.raise_for_status()


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_username: str = "",
        smtp_password: str = "",
        smtp_use_tls: bool = True,
        smtp_from: str = "",
        smtp_to: str = "",
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.smtp_use_tls = smtp_use_tls
        self.smtp_from = smtp_from
        self.smtp_to = smtp_to

    @classmethod
    def from_env(cls):
        smtp_port_str = os.getenv("SMTP_PORT", "587")
        try:
            smtp_port = int(smtp_port_str)
        except ValueError:
            smtp_port = 587

        use_tls_str = os.getenv("SMTP_USE_TLS", "true").lower()
        smtp_use_tls = use_tls_str in ("true", "1", "yes")

        return cls(
            smtp_host=os.getenv("SMTP_HOST", ""),
            smtp_port=smtp_port,
            smtp_username=os.getenv("SMTP_USERNAME", ""),
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            smtp_use_tls=smtp_use_tls,
            smtp_from=os.getenv("SMTP_FROM", ""),
            smtp_to=os.getenv("SMTP_TO", ""),
        )

    def send(self, subject: str, body: str, html_body: str = None):
        if not self.smtp_host:
            EMAIL_LOGGER.info("SMTP host not configured. Email suppressed. Subject: %s", subject)
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.smtp_from
            msg["To"] = self.smtp_to

            msg.attach(MIMEText(body, "plain"))
            if html_body:
                msg.attach(MIMEText(html_body, "html"))

            # Set up server connection
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
            if self.smtp_use_tls:
                server.starttls()

            if self.smtp_username and self.smtp_password:
                server.login(self.smtp_username, self.smtp_password)

            # Send the email
            recipients = [r.strip() for r in self.smtp_to.split(",") if r.strip()]
            server.sendmail(self.smtp_from, recipients, msg.as_string())
            server.quit()
            EMAIL_LOGGER.info("Email sent successfully: %s", subject)
        except Exception as e:
            EMAIL_LOGGER.error("Failed to send email notifier alert: %s", e)
