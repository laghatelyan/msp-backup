from email.message import EmailMessage
from email.utils import getaddresses
from datetime import datetime, timezone
import getpass
import platform
import smtplib
import ssl
import subprocess
import sys

from .common import BackupError, ETC, RUN_REPORT_FILE, SMTP_CONFIG_FILE, SMTP_PASSWORD_CRED, VERSION, decrypt_credential, encrypt_credential, fail, info, input_default, logger, read_json, remove_runtime, runtime_dir, save_json, ui, write_logrotate, yes_no

class NotificationManager:
    """Own notifications configuration and operational workflows."""

    def configure_log_rotation(self):
        frequency = ui.select("LOG ROTATION", [
            ("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"), ("0", "Cancel")
        ], "Use ↑/↓ or left/right, then Enter.")
        if frequency in (None, "0"):
            return
        while True:
            count_text = input_default("Number of archives to keep (1–365)", "14")
            if count_text.isdecimal() and 1 <= int(count_text) <= 365:
                break
            ui.status("Enter a number from 1 to 365.", "error")
        write_logrotate(frequency, int(count_text))
        info(f"Log rotation: {frequency}, archives: {count_text}.")

    def configure_notifications(self):
        ETC.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not yes_no("Enable SMTP notifications?", default=True):
            save_json(SMTP_CONFIG_FILE, {"enabled": False})
            SMTP_PASSWORD_CRED.unlink(missing_ok=True)
            info("Notifications are disabled.")
            return
        while True:
            host = input("SMTP server: ").strip()
            if host and not any(ch.isspace() for ch in host) and "=" not in host:
                break
            ui.status("Enter a valid SMTP server name.", "error")
        tls_choice = ui.select("SMTP ENCRYPTION", [
            ("1", "STARTTLS", "usually port 587"),
            ("2", "SSL/TLS", "usually port 465"),
            ("0", "Cancel"),
        ], "Use ↑/↓ or left/right, then Enter.")
        if tls_choice in (None, "0"):
            return
        tls = "starttls" if tls_choice == "1" else "ssl"
        while True:
            port = input_default("SMTP port", "587" if tls == "starttls" else "465")
            if port.isdecimal() and 1 <= int(port) <= 65535:
                break
            ui.status("Enter a port from 1 to 65535.", "error")
        auth = yes_no("Use SMTP authentication?", default=True)
        username = ""
        if auth:
            while True:
                username = input("SMTP username: ").strip()
                secret = getpass.getpass("SMTP password: ")
                if username and secret:
                    break
                ui.status("SMTP username and password are required.", "error")
            encrypt_credential("msp-backup-smtp", secret.encode(), SMTP_PASSWORD_CRED)
        else:
            SMTP_PASSWORD_CRED.unlink(missing_ok=True)
        while True:
            sender = input("Sender email address: ").strip()
            recipients = input("Recipient email addresses, comma-separated: ").strip()
            parsed_sender = [address for _, address in getaddresses([sender]) if address]
            parsed_recipients = [address for _, address in getaddresses([recipients]) if address]
            if len(parsed_sender) == 1 and parsed_recipients and all("@" in address for address in (*parsed_sender, *parsed_recipients)):
                break
            ui.status("Enter a valid sender and at least one recipient email address.", "error")
        mode_choice = ui.select("EMAIL NOTIFICATIONS", [
            ("1", "Always", "after successful and failed backups"),
            ("2", "Only on failures"),
            ("0", "Cancel"),
        ], "Use ↑/↓ or left/right, then Enter.", initial=1)
        if mode_choice in (None, "0"):
            return
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
            database = report.get("database") or {}
            if database:
                report_lines.append(f"Databases: {database.get('count', 0)} ({database.get('engine', 'unknown')})")
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
