from email.message import EmailMessage
from email.utils import getaddresses
from datetime import datetime, timezone
import getpass
import platform
import smtplib
import ssl
import subprocess
import sys

from .common import BackupError, ETC, RUN_REPORT_FILE, SMTP_CONFIG_FILE, SMTP_PASSWORD_CRED, VERSION, decrypt_credential, encrypt_credential, fail, info, input_default, logger, read_json, remove_runtime, runtime_dir, save_json, write_logrotate, yes_no

class NotificationManager:
    """Own notifications configuration and operational workflows."""

    def configure_log_rotation(self):
        frequency = input_default("Rotation frequency [daily/weekly/monthly]", "daily")
        if frequency not in ("daily", "weekly", "monthly"):
            fail("Choose daily, weekly, or monthly.")
        count_text = input_default("Number of archives to keep (1–365)", "14")
        if not count_text.isdecimal() or not 1 <= int(count_text) <= 365:
            fail("The number of archives must be between 1 and 365.")
        write_logrotate(frequency, int(count_text))
        info(f"Log rotation: {frequency}, archives: {count_text}.")

    def configure_notifications(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not yes_no("Enable SMTP notifications?", default=True):
            save_json(SMTP_CONFIG_FILE, {"enabled": False})
            SMTP_PASSWORD_CRED.unlink(missing_ok=True)
            info("Notifications are disabled.")
            return
        host = input("SMTP server: ").strip()
        if not host or any(ch.isspace() for ch in host) or "=" in host:
            fail("Invalid SMTP host.")
        tls_choice = input_default("TLS: 1=STARTTLS/587, 2=SSL-TLS/465", "1")
        if tls_choice not in ("1", "2"):
            fail("Choose STARTTLS or SSL/TLS.")
        tls = "starttls" if tls_choice == "1" else "ssl"
        port = input_default("SMTP port", "587" if tls == "starttls" else "465")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            fail("Invalid SMTP port.")
        auth = yes_no("Use SMTP authentication?", default=True)
        username = ""
        if auth:
            username = input("SMTP username: ").strip()
            secret = getpass.getpass("SMTP password: ")
            if not username or not secret:
                fail("SMTP username and password are required.")
            encrypt_credential("msp-backup-smtp", secret.encode(), SMTP_PASSWORD_CRED)
        else:
            SMTP_PASSWORD_CRED.unlink(missing_ok=True)
        sender = input("Sender email address: ").strip()
        recipients = input("Recipient email addresses, comma-separated: ").strip()
        if "@" not in sender or any(ch.isspace() for ch in sender) or "@" not in recipients or any(ch.isspace() for ch in recipients):
            fail("Enter valid email addresses.")
        mode_choice = input_default("1=Always, 2=Only on failures", "2")
        if mode_choice not in ("1", "2"):
            fail("Choose 1 or 2.")
        data = {"enabled": True, "host": host, "port": int(port), "tls": tls, "auth": auth,
                "user": username, "from": sender, "to": recipients, "mode": "always" if mode_choice == "1" else "failures"}
        save_json(SMTP_CONFIG_FILE, data)
        if yes_no("Send a test email now?", default=True):
            notify_event("test")

    def notify_event(self, event):
        config = read_json(SMTP_CONFIG_FILE, {})
        if not config.get("enabled"):
            return
        if event == "success" and config.get("mode") != "always":
            return
        host_name = subprocess.run(["hostname", "-f"], capture_output=True, text=True).stdout.strip() or platform.node()
        journal = subprocess.run(["journalctl", "-u", "msp-backup.service", "-n", "40", "--no-pager"], capture_output=True, text=True).stdout.strip()
        try:
            report = read_json(RUN_REPORT_FILE, {})
        except BackupError as exc:
            logger.warning("Could not read the last-run report: %s", exc)
            report = {}
        report_lines = [f"Server: {host_name}", f"Reported at: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}"]
        if report:
            report_lines.append(f"Latest Restic run: {report.get('result', 'unknown')}")
            if report.get("started_at"):
                report_lines.append(f"Started: {report['started_at']}")
            if report.get("finished_at"):
                report_lines.append(f"Finished: {report['finished_at']}")
            if report.get("duration_seconds") is not None:
                report_lines.append(f"Duration: {report['duration_seconds']} seconds")
            snapshot = report.get("snapshot") or {}
            snapshot_id = snapshot.get("short_id") or snapshot.get("id")
            if snapshot_id:
                report_lines.append(f"Snapshot: {snapshot_id}")
            if snapshot.get("time"):
                report_lines.append(f"Snapshot time: {snapshot['time']}")
            if report.get("error"):
                report_lines.append(f"Restic error: {report['error']}")
        if event == "failure":
            body = "The backup service failed.\n" + "\n".join(report_lines)
            if journal:
                body += f"\n\nRecent service log:\n{journal}"
            subject = f"[MSP Backup] Failure on {host_name}"
        elif event == "test":
            subject = "[MSP Backup] SMTP test"
            body = f"This is an MSP Backup test email.\nServer: {host_name}"
        else:
            subject = "[MSP Backup] Backup completed"
            body = "The backup service completed successfully.\n" + "\n".join(report_lines)
            if journal:
                body += f"\n\nRecent service log:\n{journal}"
            body += f"\n\nMSP Backup version: {VERSION}"
        work = runtime_dir()
        try:
            if config.get("auth"):
                password_path = work / "smtp-password"
                decrypt_credential("msp-backup-smtp", SMTP_PASSWORD_CRED, password_path)
                password = password_path.read_text().rstrip("\n")
            else:
                password = ""
            message = EmailMessage()
            message["From"] = config["from"]
            recipients = [address for _, address in getaddresses([config["to"]]) if address]
            if not recipients:
                raise BackupError("No SMTP recipients are configured.")
            message["To"] = ", ".join(recipients)
            message["Subject"] = subject
            message.set_content(body)
            context = ssl.create_default_context()
            if config["tls"] == "ssl":
                server = smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
            else:
                server = smtplib.SMTP(config["host"], config["port"], timeout=30)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            try:
                if config.get("auth"):
                    server.login(config["user"], password)
                server.send_message(message, to_addrs=recipients)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            info(f"SMTP notification sent ({event}).")
        except Exception as exc:
            logger.exception("SMTP notification failed")
            print(f"Could not send SMTP notification: {exc}", file=sys.stderr)
        finally:
            remove_runtime(work)


notification_manager = NotificationManager()
configure_log_rotation = notification_manager.configure_log_rotation
configure_notifications = notification_manager.configure_notifications
notify_event = notification_manager.notify_event
