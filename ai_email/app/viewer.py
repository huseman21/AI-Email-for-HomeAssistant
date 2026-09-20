"""Small local email viewer for the AI Email add-on."""

from __future__ import annotations

import email
import html
import json
import logging
import re
import smtplib
import socket
import ssl
import imaplib
import urllib.error
import urllib.request
from email import policy
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.parse import parse_qs

VIEWER_VERSION = "1.4.5"
LOGGER = logging.getLogger("ai_email.viewer")


def _message_path(root: Path, message_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", message_id):
        raise ValueError("Invalid message id")
    path = (root / f"{message_id}.eml").resolve()
    if path.parent != root.resolve():
        raise ValueError("Invalid message path")
    return path


def _header(message: Message, name: str) -> str:
    return str(message.get(name, ""))


def _decoded_header(message: Message, name: str) -> str:
    value = message.get(name)
    return str(make_header(decode_header(value))) if value else ""


def _email_address(value: str) -> str:
    _, address = parseaddr(value)
    return address.strip()


def _body_parts(message: Message) -> tuple[str, list[tuple[str, str, bytes, str]]]:
    html_body = ""
    plain_body = ""
    attachments: list[tuple[str, str, bytes, str]] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        content_type = part.get_content_type()
        content_id = part.get("Content-ID", "").strip("<>")
        filename = part.get_filename() or "attachment"
        if content_type == "text/html" and not html_body:
            html_body = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif content_type == "text/plain" and not plain_body:
            plain_body = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif part.get_content_disposition() in {"inline", "attachment"} or content_id:
            attachments.append((content_id, filename, payload, content_type))
    if not html_body:
        html_body = f"<pre>{html.escape(plain_body)}</pre>"
    return html_body, attachments


def _safe_html(body: str, message_id: str, route_prefix: str = "") -> str:
    body = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", body)
    body = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", "", body)
    body = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(['\"]).*?\1", "", body)
    body = re.sub(r"(?i)\s+(?:href|src)\s*=\s*(['\"])\s*javascript:.*?\1", "", body)
    body = body.replace(
        "cid:",
        f"{message_id}/attachment?cid=",
    )
    return body


class EmailViewerHandler(BaseHTTPRequestHandler):
    root: Path
    event_config: dict[str, str] = {}
    route_prefix: str = ""

    def _send(self, status: int, content_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/").endswith("/status"):
            self._status()
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            email_index = parts.index("email")
        except ValueError:
            email_index = -1
        route_parts = parts[email_index:] if email_index >= 0 else []
        prefix_parts = parts[:email_index] if email_index > 0 else (parts if not route_parts else [])
        self.route_prefix = "/" + "/".join(prefix_parts) if prefix_parts else ""
        try:
            if parts and parts[-1] == "config":
                self._config_page()
                return
            if not route_parts:
                self._index()
            elif len(route_parts) == 2:
                self._email_page(route_parts[1])
            elif len(route_parts) == 3 and route_parts[2] == "raw":
                self._raw(route_parts[1])
            elif len(route_parts) == 3 and route_parts[2] == "attachment":
                self._attachment(route_parts[1], parsed.query)
            else:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Email not found",
                )
        except (OSError, ValueError, LookupError):
            self._send(
                HTTPStatus.NOT_FOUND,
                "text/plain; charset=utf-8",
                b"Email not found",
            )

    def _status(self) -> None:
        llm_provider = self.event_config.get("llm_provider", "ollama")
        llm_url = self.event_config.get("llm_base_url", "")
        llm_online = False
        try:
            if llm_url:
                endpoint = llm_url.rstrip("/") + (
                    "/api/tags" if llm_provider == "ollama" else "/models"
                )
                headers = {}
                api_key = self.event_config.get("llm_api_key", "").strip()
                if api_key:
                    headers["Authorization"] = "Bearer " + api_key
                request = urllib.request.Request(endpoint, headers=headers, method="GET")
                with urllib.request.urlopen(request, timeout=3):
                    llm_online = True
        except (OSError, ValueError, urllib.error.URLError):
            pass

        imap_online = False
        try:
            host = self.event_config.get("imap_host", "")
            port = int(self.event_config.get("imap_port", "143"))
            if host:
                with socket.create_connection((host, port), timeout=3):
                    imap_online = True
        except (OSError, ValueError):
            pass

        payload = json.dumps(
            {
                "llm": {"online": llm_online, "label": "LLM"},
                "imap": {"online": imap_online, "label": "Email"},
            }
        ).encode("utf-8")
        self._send(HTTPStatus.OK, "application/json; charset=utf-8", payload)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            email_index = parts.index("email")
        except ValueError:
            email_index = -1
        route_parts = parts[email_index:] if email_index >= 0 else []
        prefix_parts = parts[:email_index] if email_index > 0 else []
        route_prefix = "/" + "/".join(prefix_parts) if prefix_parts else ""
        try:
            if parts and parts[-1] == "delete-excluded":
                self._delete_excluded()
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "./")
                self.end_headers()
                return
            if parts and parts[-1] == "config":
                self._save_config()
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "config")
                self.end_headers()
                return
            if not route_parts:
                self._delete_all()
            elif len(route_parts) == 2:
                self._delete_one(route_parts[1])
            elif len(route_parts) == 3 and route_parts[2] == "reply":
                self._reply(route_parts[1])
            elif len(route_parts) == 3 and route_parts[2] == "allow-sender":
                self._allow_sender(route_parts[1])
            elif len(route_parts) == 3 and route_parts[2] == "mark-excluded":
                self._mark_excluded(route_parts[1])
            else:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    "text/plain; charset=utf-8",
                    b"Email not found",
                )
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            if len(route_parts) == 3 and route_parts[2] in {
                "reply",
                "allow-sender",
                "mark-excluded",
            }:
                # Keep the browser's Home Assistant ingress prefix. The
                # ingress proxy may strip that prefix before forwarding the
                # request, so an absolute /email/ redirect can escape ingress.
                destination = "../../"
            elif len(route_parts) == 2:
                destination = f"{route_prefix}/" if route_prefix else "../"
            else:
                destination = f"{route_prefix}/" if route_prefix else "./"
            self.send_header("Location", destination)
            self.end_headers()
        except FileNotFoundError:
            self._send(
                HTTPStatus.NOT_FOUND,
                "text/plain; charset=utf-8",
                b"Email not found",
            )
        except ValueError as error:
            self._send_error(HTTPStatus.BAD_REQUEST, str(error))
        except (OSError, smtplib.SMTPException, RuntimeError) as error:
            self._send_error(HTTPStatus.BAD_GATEWAY, str(error))

    def _config_page(self) -> None:
        settings_path = Path(self.event_config.get("settings_path", str(self.root.parent / "settings.json")))
        criteria = self.event_config.get("classification_criteria", "")
        settings = self._read_settings(settings_path)
        criteria = str(settings.get("classification_criteria", criteria))
        approved = sorted(
            str(value) for value in settings.get("approved_senders", [])
            if isinstance(value, str) and value.strip()
        )
        sender_rows = "".join(
            f'<li><span>{html.escape(sender)}</span>'
            f'<button class="remove" type="submit" name="remove_sender" '
            f'value="{html.escape(sender, quote=True)}">Remove</button></li>'
            for sender in approved
        ) or '<li class="empty">No approved senders yet.</li>'
        content = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AI Email Settings</title><style>
body{{margin:0;padding-bottom:42px;background:#f4f6f8;font:15px system-ui,sans-serif;color:#202124}}
main{{max-width:900px;margin:32px auto;background:#fff;border:1px solid #e1e5e9;
border-radius:16px;box-shadow:0 8px 30px #20212412;overflow:hidden}}
header{{padding:30px 34px 24px;background:linear-gradient(135deg,#f8fbff,#fff);
border-bottom:1px solid #edf0f2}} h1{{margin:0 0 8px;font-size:28px}}
.subtitle{{margin:0;color:#5f6368}} section{{padding:26px 34px;border-bottom:1px solid #edf0f2}}
h2{{margin:0 0 6px;font-size:19px}} .help{{margin:0 0 16px;color:#6b7280;line-height:1.5}}
textarea{{width:100%;box-sizing:border-box;border:1px solid #c4c7c5;border-radius:8px;
padding:12px;font:inherit;line-height:1.5;resize:vertical}} textarea:focus{{outline:2px solid #a8c7fa;
border-color:#1a73e8}} button{{border:0;border-radius:8px;padding:10px 16px;
font:600 14px system-ui;cursor:pointer}} .save{{margin-top:14px;background:#1a73e8;color:white}}
.save:hover{{background:#1557b0}} ul{{list-style:none;padding:0;margin:0;border:1px solid #e1e5e9;
border-radius:10px;overflow:hidden}} li{{display:flex;justify-content:space-between;align-items:center;
gap:14px;padding:13px 15px;border-bottom:1px solid #edf0f2}} li:last-child{{border-bottom:0}}
li span{{overflow-wrap:anywhere}} .remove{{padding:7px 11px;color:#c5221f;background:#fff;
border:1px solid #f28b82;white-space:nowrap}} .remove:hover{{background:#fce8e6}}
.empty{{display:block;color:#6b7280;text-align:center;font-style:italic}}
footer{{padding:20px 34px}} footer a{{color:#1967d2;text-decoration:none;font-weight:600}}
</style></head><body><main><header><h1>Settings</h1>
<p class="subtitle">Manage how AI Email classifies and handles incoming messages.</p></header>
<section><h2>Classification rules</h2><p class="help">These instructions are sent to the local AI model.
Direct personal messages and important financial or security messages should be included.</p>
<form method="post" action="config"><textarea name="classification_criteria" rows="9"
required>{html.escape(criteria)}</textarea><br><button class="save" type="submit">Save classification rules</button></form></section>
<section><h2>Always-allowed senders</h2><p class="help">Messages from these addresses bypass AI classification and are always placed in Included / Allowed.</p>
<form method="post" action="config"><ul>{sender_rows}</ul></form></section>
<footer><a href="./">← Back to AI Email</a></footer></main></body></html>"""
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", content.encode("utf-8"))

    def _save_config(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        remove_sender = form.get("remove_sender", [""])[0].strip().lower()
        criteria = form.get("classification_criteria", [""])[0].strip()
        settings_path = Path(self.event_config.get("settings_path", str(self.root.parent / "settings.json")))
        settings = self._read_settings(settings_path)
        if remove_sender:
            approved = settings.get("approved_senders", [])
            settings["approved_senders"] = [
                value for value in approved
                if str(value).strip().lower() != remove_sender
            ] if isinstance(approved, list) else []
        elif criteria:
            settings["classification_criteria"] = criteria
        else:
            raise ValueError("Classification criteria cannot be empty")
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

    def _read_settings(self, settings_path: Path) -> dict[str, object]:
        try:
            value = json.loads(settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _allow_sender(self, message_id: str) -> None:
        message = self._load(message_id)
        sender = _email_address(_decoded_header(message, "From")).lower()
        if not sender:
            raise ValueError("This email does not contain a sender address")
        settings_path = Path(
            self.event_config.get("settings_path", str(self.root.parent / "settings.json"))
        )
        settings = self._read_settings(settings_path)
        approved = settings.get("approved_senders", [])
        approved_senders = {
            str(value).strip().lower() for value in approved
        } if isinstance(approved, list) else set()
        approved_senders.add(sender)
        settings["approved_senders"] = sorted(approved_senders)
        settings_path.write_text(json.dumps(settings), encoding="utf-8")

        metadata = self._metadata(message_id)
        metadata["status"] = "important"
        metadata["summary"] = "Sender manually added to the always-allowed list."
        metadata_path = self.root / f"{message_id}.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        message_header_id = _decoded_header(message, "Message-ID")
        try:
            self._publish_deleted(message_id, message_header_id)
            self._publish_processed(message_id, message, metadata["summary"])
        except (OSError, urllib.error.URLError) as error:
            LOGGER.error("Could not update Home Assistant after allowing %s: %s", message_id, error)

    def _mark_excluded(self, message_id: str) -> None:
        message = self._load(message_id)
        metadata = self._metadata(message_id)
        metadata["status"] = "excluded"
        metadata["summary"] = "Email manually marked as excluded."
        metadata_path = self.root / f"{message_id}.json"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        message_header_id = _decoded_header(message, "Message-ID")
        try:
            self._publish_deleted(message_id, message_header_id)
            self._publish_processed(
                message_id,
                message,
                metadata["summary"],
                status="excluded",
            )
        except (OSError, urllib.error.URLError) as error:
            LOGGER.error(
                "Could not update Home Assistant after marking %s excluded: %s",
                message_id,
                error,
            )

    def _send_error(self, status: int, message: str) -> None:
        content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AI Email error</title></head>
<body><h1>AI Email could not complete the request</h1>
<p>{html.escape(message)}</p><p><a href="../">Back to email</a></p></body></html>"""
        self._send(status, "text/html; charset=utf-8", content.encode("utf-8"))

    def _delete_all(self) -> None:
        for path in self.root.glob("*.eml"):
            self._delete_from_imap(path.stem)
            try:
                with path.open("rb") as file:
                    message = email.message_from_binary_file(file, policy=policy.default)
                try:
                    self._publish_deleted(path.stem, _decoded_header(message, "Message-ID"))
                except (OSError, urllib.error.URLError) as error:
                    LOGGER.error("Could not update Home Assistant after deleting %s: %s", path.stem, error)
            except (OSError, ValueError):
                pass
            path.unlink()
            path.with_suffix(".json").unlink(missing_ok=True)

    def _delete_excluded(self) -> None:
        for path in self.root.glob("*.eml"):
            metadata = self._metadata(path.stem)
            if str(metadata.get("status", "")).lower() != "excluded":
                continue
            try:
                with path.open("rb") as file:
                    message = email.message_from_binary_file(file, policy=policy.default)
                self._delete_from_imap(path.stem)
                try:
                    self._publish_deleted(
                        path.stem, _decoded_header(message, "Message-ID")
                    )
                except (OSError, urllib.error.URLError) as error:
                    LOGGER.error(
                        "Could not update Home Assistant after deleting excluded %s: %s",
                        path.stem,
                        error,
                    )
                path.unlink()
                path.with_suffix(".json").unlink(missing_ok=True)
            except (OSError, ValueError):
                LOGGER.exception("Could not delete excluded email %s", path.stem)

    def _delete_one(self, message_id: str) -> None:
        path = _message_path(self.root, message_id)
        with path.open("rb") as file:
            message = email.message_from_binary_file(file, policy=policy.default)
        self._delete_from_imap(message_id)
        path.unlink()
        path.with_suffix(".json").unlink(missing_ok=True)
        try:
            self._publish_deleted(message_id, _decoded_header(message, "Message-ID"))
        except (OSError, urllib.error.URLError) as error:
            LOGGER.error("Could not update Home Assistant after deleting %s: %s", message_id, error)

    def _delete_from_imap(self, viewer_id: str) -> None:
        metadata_path = self.root / f"{viewer_id}.json"
        if not metadata_path.exists():
            LOGGER.warning("No IMAP metadata found for viewer email %s", viewer_id)
            return
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        uid = str(metadata["uid"])
        host = self.event_config.get("imap_host", "")
        username = self.event_config.get("imap_username", "")
        password = self.event_config.get("imap_password", "")
        if not host or not username or not password:
            raise RuntimeError("IMAP settings are not available for server deletion")
        context = ssl.create_default_context()
        if self.event_config.get("imap_verify_ssl", "false") != "true":
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        security = self.event_config.get("imap_security", "starttls")
        port = int(self.event_config.get("imap_port", "143"))
        if security == "ssl":
            connection = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=30)
        else:
            connection = imaplib.IMAP4(host, port, timeout=30)
            if security == "starttls":
                connection.starttls(ssl_context=context)
        try:
            connection.login(username, password)
            mailbox = metadata.get("mailbox") or self.event_config.get("imap_mailbox", "INBOX")
            result, _ = connection.select(mailbox)
            if result != "OK":
                raise RuntimeError(f"Could not select IMAP mailbox {mailbox}")
            result, _ = connection.uid("store", uid, "+FLAGS.SILENT", r"(\Deleted)")
            if result != "OK":
                raise RuntimeError(f"Could not mark IMAP UID {uid} for deletion")
            result, _ = connection.expunge()
            if result != "OK":
                raise RuntimeError(f"Could not expunge IMAP UID {uid}")
            LOGGER.info("Deleted IMAP UID %s from mailbox %s", uid, mailbox)
        finally:
            try:
                connection.close()
            finally:
                connection.logout()

    def _reply(self, message_id: str) -> None:
        message = self._load(message_id)
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        body = form.get("body", [""])[0].strip()
        recipient = form.get("recipient", [""])[0].strip()
        if not body:
            raise ValueError("Reply body is required")
        if not recipient:
            recipient = _email_address(
                _decoded_header(message, "Reply-To") or _decoded_header(message, "From")
            )
        if not recipient:
            raise ValueError("The email has no reply recipient")
        if "@" not in recipient or any(char.isspace() for char in recipient):
            raise ValueError("Enter a complete recipient email address")
        subject = _decoded_header(message, "Subject")
        reply = EmailMessage()
        sender = _email_address(
            self.event_config.get("smtp_from")
            or self.event_config.get("imap_username", "")
        )
        if "@" not in sender:
            sender = _email_address(self.event_config.get("imap_username", ""))
        if not sender or "@" not in sender or any(char.isspace() for char in sender):
            raise ValueError(
                "Configure smtp_from or imap_username as a complete email address, "
                "such as huseman@huseman.co"
            )
        reply["From"] = sender
        reply["To"] = recipient
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        reply["In-Reply-To"] = _decoded_header(message, "Message-ID")
        reply["References"] = _decoded_header(message, "References") + " " + reply["In-Reply-To"]
        reply.set_content(body)
        self._send_smtp(reply)

    def _send_smtp(self, message: EmailMessage) -> None:
        host = self.event_config.get("smtp_host", "")
        port = int(self.event_config.get("smtp_port", "25"))
        security = self.event_config.get("smtp_security", "none")
        use_tls = self.event_config.get("smtp_use_tls", "false").lower() == "true"
        if not use_tls:
            security = "none"
        elif security == "none":
            security = "starttls"
        username = self.event_config.get("smtp_username", "")
        password = self.event_config.get("smtp_password", "")
        if not host:
            raise RuntimeError("Configure smtp_host in the AI Email app settings")
        LOGGER.info(
            "Connecting to SMTP server %s:%s using %s",
            host,
            port,
            security,
        )
        if security == "ssl":
            connection = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            connection = smtplib.SMTP(host, port, timeout=30)
        try:
            connection.ehlo()
            if security == "starttls":
                connection.starttls()
                connection.ehlo()
            if username and password:
                LOGGER.debug("Authenticating with SMTP username %s", username)
                connection.login(username, password)
            connection.send_message(message)
            LOGGER.info(
                "Reply sent successfully to %s with subject %s",
                message["To"],
                message["Subject"],
            )
        except (OSError, smtplib.SMTPException) as error:
            LOGGER.error(
                "Outgoing SMTP delivery failed to %s via %s:%s: %s",
                message["To"],
                host,
                port,
                error,
            )
            raise
        finally:
            connection.quit()

    def _publish_deleted(self, viewer_id: str, message_id: str) -> None:
        if not viewer_id:
            return
        token = self.event_config.get("token", "")
        if not token:
            return
        payload = json.dumps(
            {"viewer_id": viewer_id, "message_id": message_id}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.event_config.get("url", ""),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

    def _publish_processed(
        self,
        viewer_id: str,
        message: Message,
        summary: object,
        status: str = "important",
    ) -> None:
        token = self.event_config.get("token", "")
        url = self.event_config.get("processed_url", "")
        if not token or not url:
            return
        message_id = _decoded_header(message, "Message-ID")
        payload = json.dumps(
            {
                "message_id": message_id,
                "sender": _decoded_header(message, "From"),
                "subject": _decoded_header(message, "Subject") or "(no subject)",
                "body": "",
                "status": status,
                "summary": str(summary),
                "viewer_id": viewer_id,
                "viewer_url": (
                    self.event_config.get("viewer_base_url", "").rstrip("/")
                    + f"/email/{viewer_id}"
                ),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

    def _index(self) -> None:
        included_rows: list[str] = []
        excluded_rows: list[str] = []
        for path in sorted(self.root.glob("*.eml"), key=lambda item: item.stat().st_mtime, reverse=True):
            message_id = path.stem
            try:
                message = self._load(message_id)
                metadata = self._metadata(message_id)
                subject = html.escape(_header(message, "Subject") or "(no subject)")
                sender = html.escape(_header(message, "From"))
                date = html.escape(_header(message, "Date"))
                status = str(metadata.get("status", "unclassified")).lower()
                status_label = html.escape(status.capitalize())
                status_class = "important" if status == "important" else "excluded" if status == "excluded" else "unknown"
                summary = html.escape(str(metadata.get("summary", "")))
                row = (
                    f'<li class="email-card"><div class="email-heading">'
                    f'<a href="email/{message_id}"><strong>{subject}</strong></a> '
                    f'<b class="status {status_class}">{status_label}</b></div>'
                    f'<span class="meta">{sender} · {date}</span>'
                    f"{f'<p class=\"summary\">{summary}</p>' if summary else ''}"
                    f'<a class="open-link" href="email/{message_id}">Open email <span aria-hidden="true">→</span></a>'
                    + (
                        f'<form class="allow-form" method="post" action="email/{message_id}/allow-sender">'
                        f'<button class="allow-button" type="submit" '
                        f'onclick="return confirm(\'Always allow messages from {html.escape(sender, quote=True)}?\');">'
                        "Always allow sender</button></form>"
                        if status == "excluded"
                        else ""
                    )
                    + (
                        f'<form class="exclude-form" method="post" action="email/{message_id}/mark-excluded">'
                        f'<button class="exclude-button" type="submit" '
                        f'onclick="return confirm(\'Mark this email as excluded?\');">'
                        "Mark as excluded</button></form>"
                        if status == "important"
                        else ""
                    )
                    + f"</li>"
                )
                if status == "important":
                    included_rows.append(row)
                else:
                    excluded_rows.append(row)
            except (OSError, ValueError):
                continue
        included_listing = "".join(included_rows) or '<li class="empty">No included emails yet.</li>'
        excluded_listing = "".join(excluded_rows) or '<li class="empty">No excluded emails yet.</li>'
        total = len(included_rows) + len(excluded_rows)
        content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AI Email</title>
<style>
body{{margin:0;padding-bottom:42px;background:#f4f6f8;font:15px system-ui,sans-serif;color:#202124}}
main{{max-width:980px;margin:32px auto;background:#fff;border:1px solid #e1e5e9;
border-radius:16px;box-shadow:0 8px 30px #20212412;overflow:hidden}}
.page-header{{padding:30px 34px 24px;background:linear-gradient(135deg,#f8fbff,#fff)}}
h1{{margin:0 0 8px;font-size:28px;letter-spacing:-.3px}}
.subtitle{{margin:0;color:#5f6368}} .toolbar{{display:flex;gap:16px;align-items:center;
flex-wrap:wrap;padding:16px 34px;border-top:1px solid #edf0f2;border-bottom:1px solid #edf0f2}}
.toolbar a{{color:#1967d2;text-decoration:none;font-weight:600;font-size:14px}}
button{{border:0;border-radius:8px;padding:10px 15px;background:#fff;color:#c5221f;
border:1px solid #dadce0;font:600 14px system-ui;cursor:pointer}}
button:hover{{background:#fce8e6;border-color:#f28b82}}
.section{{padding:26px 34px 8px}} .section + .section{{margin-top:78px;padding-top:78px;
border-top:4px solid #dfe5eb}} .section-header{{display:flex;justify-content:space-between;
align-items:center;gap:12px;margin-bottom:14px}} h2{{margin:0;font-size:19px}}
.section-description{{margin:3px 0 0;color:#6b7280;font-size:13px}}
.section-actions{{display:flex;align-items:center;gap:10px}} .count{{min-width:28px;padding:5px 10px;border-radius:999px;text-align:center;font-weight:700;
font-size:13px}} .included-count{{background:#e6f4ea;color:#137333}}
.excluded-count{{background:#fce8e6;color:#c5221f}}
ul{{list-style:none;padding:0;margin:0;border:1px solid #e1e5e9;border-radius:12px;overflow:hidden}}
li{{padding:17px 18px;border-bottom:1px solid #edf0f2}} li:last-child{{border-bottom:0}}
.email-heading{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
a{{color:#1967d2;text-decoration:none}} .email-heading a{{color:#202124;font-size:16px}}
.meta{{display:block;margin-top:6px;color:#6b7280;font-size:13px;overflow-wrap:anywhere}}
.summary{{margin:10px 0 8px;color:#3c4043;line-height:1.45}}
.open-link{{display:inline-block;font-size:13px;font-weight:600}}
.open-link span{{font-size:17px;vertical-align:-1px}} .empty{{color:#6b7280;text-align:center;
padding:26px;font-style:italic}}
.allow-form{{display:inline-block;margin:0 0 0 12px}} .allow-button{{padding:6px 10px;
font-size:12px;color:#137333;background:#fff;border:1px solid #b7dfc2}}
.allow-button:hover{{background:#e6f4ea;border-color:#81c995}}
.exclude-form{{display:inline-block;margin:0 0 0 12px}} .exclude-button{{padding:6px 10px;
font-size:12px;color:#c5221f;background:#fff;border:1px solid #f28b82}}
.exclude-button:hover{{background:#fce8e6;border-color:#e06c65}}
.status{{border-radius:999px;padding:3px 9px;font-size:12px}}
.important{{background:#e6f4ea;color:#137333}} .excluded{{background:#fce8e6;color:#c5221f}}
.unknown{{background:#f1f3f4;color:#5f6368}}
.service-bar{{position:fixed;z-index:10;bottom:0;left:0;width:100vw;box-sizing:border-box;
display:flex;justify-content:center;gap:22px;padding:7px 14px;background:#202124;
color:#e8eaed;font-size:12px;box-shadow:0 -2px 8px #0003}}
.service{{position:relative;left:5ch;display:inline-flex;align-items:center;gap:6px}}
.service-dot{{width:8px;height:8px;border-radius:50%;background:#9aa0a6}}
.service-dot.online{{background:#34a853}} .service-dot.offline{{background:#ea4335}}
</style></head><body><main>
<header class="page-header"><h1>AI Email</h1>
<p class="subtitle">Review messages classified by your local AI model.</p></header>
<div class="toolbar"><a href="config">Settings</a>
<span class="subtitle">Viewer {VIEWER_VERSION} · {total} saved messages</span>
<form method="post" onsubmit="return confirm('Delete all saved emails? This cannot be undone.');">
<button type="submit">Delete all emails</button></form></div>
<section class="section"><div class="section-header"><div><h2>Included / Allowed</h2>
<p class="section-description">Messages identified as important or requiring attention.</p></div>
<span class="count included-count">{len(included_rows)}</span></div>
<ul>{included_listing}</ul></section>
<section class="section"><div class="section-header"><div><h2>Excluded</h2>
<p class="section-description">Newsletters, promotions, automated notifications, and other filtered mail.</p></div>
<div class="section-actions">
<form method="post" action="delete-excluded" onsubmit="return confirm('Delete all excluded emails? This cannot be undone.');">
<button type="submit">Delete all excluded</button></form>
<span class="count excluded-count">{len(excluded_rows)}</span></div></div>
<ul>{excluded_listing}</ul></section>
<footer class="service-bar" aria-live="polite">
<span class="service"><i class="service-dot" id="llm-dot"></i><span id="llm-status">LLM: Checking...</span></span>
<span class="service"><i class="service-dot" id="imap-dot"></i><span id="imap-status">Email: Checking...</span></span>
</footer>
<script>
async function updateStatus() {{
  try {{
    const response = await fetch('status', {{cache: 'no-store'}});
    const status = await response.json();
    for (const name of ['llm', 'imap']) {{
      const dot = document.getElementById(name + '-dot');
      const label = document.getElementById(name + '-status');
      const online = status[name].online;
      dot.className = 'service-dot ' + (online ? 'online' : 'offline');
      label.textContent = status[name].label + ': ' + (online ? 'Connected' : 'Disconnected');
    }}
  }} catch (error) {{
    for (const name of ['llm', 'imap']) {{
      document.getElementById(name + '-dot').className = 'service-dot offline';
      document.getElementById(name + '-status').textContent =
        (name === 'llm' ? 'LLM' : 'Email') + ': Disconnected';
    }}
  }}
}}
updateStatus();
setInterval(updateStatus, 30000);
</script>
</main></body></html>"""
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", content.encode("utf-8"))

    def _load(self, message_id: str) -> Message:
        with _message_path(self.root, message_id).open("rb") as file:
            return email.message_from_binary_file(file, policy=policy.default)

    def _metadata(self, message_id: str) -> dict[str, object]:
        metadata_path = self.root / f"{message_id}.json"
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _email_page(self, message_id: str) -> None:
        try:
            message = self._load(message_id)
        except FileNotFoundError:
            self._send(
                HTTPStatus.NOT_FOUND,
                "text/plain; charset=utf-8",
                b"This email was processed before the viewer was installed. "
                b"Only newly processed emails have an original message available.",
            )
            return
        body, attachments = _body_parts(message)
        metadata = self._metadata(message_id)
        status = str(metadata.get("status", "unclassified")).lower()
        status_label = html.escape(status.capitalize())
        status_class = "important" if status == "important" else "excluded" if status == "excluded" else "unknown"
        summary = html.escape(str(metadata.get("summary", "")))
        attachment_links = "".join(
            f'<li><a href="{message_id}/attachment?name={quote(filename)}">'
            f"{html.escape(filename)}</a></li>"
            for content_id, filename, payload, content_type in attachments
            if not content_id or not content_type.startswith("image/")
        )
        attachment_section = (
            f'<section><h2>Attachments</h2><ul>{attachment_links}</ul></section>'
            if attachment_links
            else ""
        )
        content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(_header(message, "Subject"))}</title>
<style>
body{{margin:0;background:#f1f3f4;font:15px system-ui,sans-serif;color:#202124}}
main{{max-width:900px;margin:24px auto;background:white;border-radius:12px;
box-shadow:0 2px 10px #0002;overflow:hidden}}
header{{padding:22px 26px;border-bottom:1px solid #ddd}}
h1{{font-size:22px;margin:0 0 14px;overflow-wrap:anywhere}}
.status{{display:inline-block;border-radius:999px;padding:5px 11px;font-weight:600}}
.important{{background:#e6f4ea;color:#137333}} .excluded{{background:#fce8e6;color:#c5221f}}
.unknown{{background:#f1f3f4;color:#5f6368}}
dl{{display:grid;grid-template-columns:90px 1fr;gap:6px;margin:0;color:#5f6368}}
dd{{margin:0;color:#202124;overflow-wrap:anywhere}}
article{{padding:26px;overflow:auto}} article img{{max-width:100%;height:auto}}
section{{margin-top:28px;border-top:1px solid #ddd;padding-top:14px}}
a{{color:#1967d2}} .tools{{display:flex;flex-wrap:wrap;gap:14px;padding:14px 26px;
background:#f8f9fa;align-items:center}} .tools a{{font-size:14px}}
details{{margin:24px 26px 0;border:1px solid #dadce0;border-radius:10px;
overflow:hidden}} summary{{cursor:pointer;padding:14px 16px;font-weight:600;
color:#202124;list-style:none}} summary::-webkit-details-marker{{display:none}}
summary::before{{content:"+";display:inline-block;width:22px;color:#1967d2;
font-size:20px;vertical-align:-2px}} details[open] summary::before{{content:"−"}}
details form{{padding:0 16px 16px}} label{{display:block;color:#5f6368;font-size:13px;
font-weight:600}} input,textarea{{box-sizing:border-box;width:100%;margin-top:6px;
border:1px solid #c4c7c5;border-radius:7px;padding:10px 11px;font:inherit;
color:#202124;background:#fff}} input:focus,textarea:focus{{outline:2px solid #a8c7fa;
border-color:#1a73e8}} textarea{{resize:vertical;min-height:150px}}
button{{border:0;border-radius:7px;padding:10px 16px;font:600 14px system-ui,sans-serif;
cursor:pointer;transition:background .15s,box-shadow .15s,transform .15s}}
button:focus-visible{{outline:3px solid #a8c7fa;outline-offset:2px}}
button:hover{{box-shadow:0 1px 3px #0003;transform:translateY(-1px)}}
.send-button{{background:#1a73e8;color:#fff}} .send-button:hover{{background:#1557b0}}
.delete{{margin:18px 26px 0;padding-bottom:2px;text-align:right}}
.delete-button{{background:#fff;color:#c5221f;border:1px solid #dadce0}}
.delete-button:hover{{background:#fce8e6;border-color:#f28b82}}
</style></head><body><main>
<header><h1>{html.escape(_header(message, "Subject") or "(no subject)")}</h1>
<p class="status {status_class}">{status_label}</p>
{f'<p><strong>AI summary:</strong> {summary}</p>' if summary else ''}
<dl><dt>From</dt><dd>{html.escape(_header(message, "From"))}</dd>
<dt>To</dt><dd>{html.escape(_header(message, "To"))}</dd>
<dt>Date</dt><dd>{html.escape(_header(message, "Date"))}</dd></dl></header>
<div class="tools"><a href="{message_id}/raw">Download original email</a> ·
<a href="../config">Settings</a></div>
<details><summary>Reply</summary>
<form method="post" action="{message_id}/reply">
<p><label>To<br><input name="recipient" type="email" value="{html.escape(_email_address(_decoded_header(message, "Reply-To") or _decoded_header(message, "From")))}" required></label></p>
<p><label>Message<br><textarea name="body" rows="8" required></textarea></label></p>
<button class="send-button" type="submit">Send reply</button>
</form></details>
<form class="delete" method="post">
<button class="delete-button" type="submit">Delete this email</button></form>
<article>{_safe_html(body, message_id)}{attachment_section}</article>
</main></body></html>"""
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", content.encode("utf-8"))

    def _raw(self, message_id: str) -> None:
        path = _message_path(self.root, message_id)
        self._send(HTTPStatus.OK, "message/rfc822", path.read_bytes())

    def _attachment(self, message_id: str, query: str) -> None:
        message = self._load(message_id)
        requested_cid = ""
        requested_name = ""
        for item in query.split("&"):
            key, _, value = item.partition("=")
            if key == "cid":
                requested_cid = unquote(value).strip("<>")
            elif key == "name":
                requested_name = unquote(value)
        _, attachments = _body_parts(message)
        for content_id, filename, payload, content_type in attachments:
            if (requested_cid and content_id == requested_cid) or (
                requested_name and filename == requested_name
            ):
                self._send(HTTPStatus.OK, content_type, payload)
                return
        self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Attachment not found")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def start_viewer(
    root: Path,
    event_config: dict[str, str] | None = None,
    host: str = "0.0.0.0",
    port: int = 8099,
) -> ThreadingHTTPServer:
    root.mkdir(parents=True, exist_ok=True)
    handler = type(
        "ConfiguredEmailViewerHandler",
        (EmailViewerHandler,),
        {"root": root, "event_config": event_config or {}},
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
