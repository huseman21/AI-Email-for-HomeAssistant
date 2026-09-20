#!/usr/bin/env python3
"""AI Email add-on: classify unread IMAP messages with a local LLM."""

from __future__ import annotations

import asyncio
import email
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import urllib.error
import urllib.request
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from viewer import start_viewer

LOGGER = logging.getLogger("ai_email")
CONFIG_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/state.json")
EMAILS_PATH = Path("/data/emails")
SETTINGS_PATH = Path("/data/settings.json")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        return json.load(config_file)


def classification_criteria(config: dict[str, Any]) -> str:
    try:
        with SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            value = json.load(settings_file).get("classification_criteria")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return str(config["classification_criteria"])


def sender_address(value: str) -> str:
    return parseaddr(value)[1].strip().lower()


def approved_senders() -> set[str]:
    try:
        with SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            values = json.load(settings_file).get("approved_senders", [])
        if isinstance(values, list):
            return {
                sender_address(str(value))
                for value in values
                if sender_address(str(value))
            }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def excluded_senders() -> set[str]:
    try:
        with SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            values = json.load(settings_file).get("excluded_senders", [])
        if isinstance(values, list):
            return {
                sender_address(str(value))
                for value in values
                if sender_address(str(value))
            }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def text_from_message(message: Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    text_parts: list[str] = []
    for part in parts:
        if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text_parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(text_parts).strip()


def parse_json_response(response_text: str) -> dict[str, str]:
    candidate = response_text.strip()
    if not candidate:
        raise ValueError("LLM returned an empty response; check the model and prompt")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        # Some local instruct models add an explanation or truncate the final
        # closing brace despite being asked for JSON. Recover only the two
        # fields required by the classifier.
        status_match = re.search(
            r'"status"\s*:\s*"(important|excluded)"', response_text, re.IGNORECASE
        )
        summary_match = re.search(
            r'"summary"\s*:\s*"((?:\\.|[^"\\])*)"', response_text, re.DOTALL
        )
        if not status_match or not summary_match:
            raise error
        parsed = {
            "status": status_match.group(1).lower(),
            "summary": bytes(summary_match.group(1), "utf-8").decode(
                "unicode_escape"
            ),
        }
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not a JSON object")
    status = parsed.get("status")
    summary = parsed.get("summary")
    if status not in {"important", "excluded"} or not isinstance(summary, str):
        raise ValueError("LLM JSON must contain a valid status and summary")
    return {"status": status, "summary": summary[:500]}


def load_state() -> dict[str, Any]:
    try:
        with STATE_PATH.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
        return {
            "processed_uids": list(state.get("processed_uids", [])),
            "processed_message_ids": list(state.get("processed_message_ids", [])),
        }
    except FileNotFoundError:
        return {"processed_uids": [], "processed_message_ids": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = STATE_PATH.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file)
    temporary_path.replace(STATE_PATH)


def read_message(connection: Any, uid: bytes) -> dict[str, Any] | None:
    result, data = connection.uid("fetch", uid, "(BODY.PEEK[])")
    if result != "OK" or not data:
        LOGGER.warning("Could not fetch IMAP UID %s", uid.decode())
        return None
    raw_message = next((item[1] for item in data if isinstance(item, tuple)), None)
    if not raw_message:
        return None
    message = email.message_from_bytes(raw_message)
    return {
        "message_id": decode_mime_header(message.get("Message-ID")) or uid.decode(),
        "sender": decode_mime_header(message.get("From")),
        "subject": decode_mime_header(message.get("Subject")) or "(no subject)",
        "body": text_from_message(message)[:12000],
        "raw": raw_message,
    }


def fetch_unread(
    config: dict[str, Any],
    processed_uids: set[str],
    processed_message_ids: set[str],
) -> list[dict[str, str]]:
    import imaplib

    security = str(config.get("imap_security", "starttls")).lower()
    host = config["imap_host"]
    port = int(config["imap_port"])
    timeout = int(config.get("imap_timeout", 30))
    context = ssl.create_default_context()
    if not bool(config.get("imap_verify_ssl", False)):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    connection: Any | None = None
    try:
        if security == "ssl":
            connection = imaplib.IMAP4_SSL(
                host, port, ssl_context=context, timeout=timeout
            )
        elif security == "starttls":
            connection = imaplib.IMAP4(host, port, timeout=timeout)
            connection.starttls(ssl_context=context)
        elif security == "none":
            connection = imaplib.IMAP4(host, port, timeout=timeout)
        else:
            raise RuntimeError(
                f"Unsupported imap_security {security!r}; use ssl, starttls, or none"
            )
        connection.login(config["imap_username"], config["imap_password"])
        result, _ = connection.select(config["imap_mailbox"], readonly=True)
        if result != "OK":
            raise RuntimeError(f"Could not select mailbox {config['imap_mailbox']}")
        result, data = connection.uid("search", None, "UNSEEN")
        if result != "OK":
            raise RuntimeError("Could not search unread IMAP messages")
        messages: list[dict[str, Any]] = []
        batch_message_ids: set[str] = set()
        for uid in data[0].split():
            uid_text = uid.decode()
            if uid_text in processed_uids:
                continue
            message = read_message(connection, uid)
            if message:
                if message["message_id"] in processed_message_ids or message["message_id"] in batch_message_ids:
                    LOGGER.debug(
                        "Skipping already processed Message-ID %s (UID %s)",
                        message["message_id"],
                        uid_text,
                    )
                    continue
                message["uid"] = uid_text
                message["mailbox"] = config["imap_mailbox"]
                messages.append(message)
                batch_message_ids.add(message["message_id"])
        return messages
    except ConnectionRefusedError as error:
        raise RuntimeError(
            f"IMAP connection refused by {host}:{port}. Verify that the IMAP "
            "service is running, the port is enabled, and the add-on can reach "
            "that address from its network."
        ) from error
    except socket.timeout as error:
        raise RuntimeError(
            f"Timed out connecting to IMAP server {host}:{port}. Verify the "
            "server address, port, firewall, and add-on network access."
        ) from error
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            if connection is not None:
                connection.logout()


async def http_json(
    url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    def request() -> dict[str, Any]:
        request_data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            url,
            data=request_data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=90) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP request to {url} failed ({error.code}): {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"HTTP request to {url} failed: {error.reason}") from error

    return await asyncio.to_thread(request)


async def classify(config: dict[str, Any], message: dict[str, str]) -> dict[str, str]:
    sender = sender_address(message["sender"])
    if sender and sender in excluded_senders():
        return {
            "status": "excluded",
            "summary": "Sender is on the always-excluded list.",
        }
    if sender and sender in approved_senders():
        return {
            "status": "important",
            "summary": "Sender is on the always-allowed list.",
        }
    prompt = (
        "Analyze this email. Determine if it is 'important' or 'excluded' based on "
        f"these criteria: {classification_criteria(config)}\n\n"
        f"Sender: {message['sender']}\nSubject: {message['subject']}\n"
        f"Body:\n{message['body']}\n\n"
        "Respond strictly in valid JSON format with double quotes: "
        '{"status":"important" or "excluded","summary":"short summary"}'
    )
    provider = str(config.get("llm_provider", "ollama")).strip().lower()
    if provider == "home_assistant":
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
        api_token = str(config.get("homeassistant_api_token", "")).strip()
        token = supervisor_token or api_token
        if not token:
            raise RuntimeError(
                "Home Assistant conversation access requires the add-on Supervisor "
                "token or a configured homeassistant_api_token"
            )
        endpoint = (
            "http://supervisor/core/api/conversation/process"
            if supervisor_token
            else "http://homeassistant:8123/api/conversation/process"
        )
        request_payload: dict[str, Any] = {
            "text": prompt,
            "language": "en",
        }
        agent_id = str(config.get("homeassistant_conversation_agent", "")).strip()
        if agent_id:
            request_payload["agent_id"] = agent_id
        payload = await http_json(
            endpoint,
            request_payload,
            headers={"Authorization": "Bearer " + token},
        )
        response = payload.get("response")
        response_text = ""
        if isinstance(response, dict):
            speech = response.get("speech")
            if isinstance(speech, dict):
                plain = speech.get("plain")
                if isinstance(plain, dict) and isinstance(plain.get("speech"), str):
                    response_text = plain["speech"]
        if not response_text:
            raise ValueError(
                "Home Assistant conversation agent returned no response text. "
                f"Response keys: {sorted(payload)}"
            )
        return parse_json_response(response_text)
    base_url = str(config.get("llm_base_url", "")).strip()
    model = str(config.get("llm_model", "")).strip()
    if not base_url:
        base_url = str(config.get("ollama_url", "")).strip()
    if not model:
        model = str(config.get("ollama_model", "")).strip()
    if not base_url or not model:
        raise RuntimeError("Configure an LLM base URL and model in the AI Email app")

    headers: dict[str, str] = {}
    api_key = str(config.get("llm_api_key", "")).strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider == "ollama":
        endpoint = f"{base_url.rstrip('/')}/api/generate"
        request_payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
    elif provider == "openai_compatible":
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        request_payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only one complete JSON object. Do not use Markdown, "
                        "code fences, explanations, or any text before or after it."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "max_tokens": 120,
        }
    else:
        raise RuntimeError(
            f"Unsupported llm_provider {provider!r}; use ollama, openai_compatible, "
            "or home_assistant"
        )

    payload = await http_json(endpoint, request_payload, headers=headers)
    if provider == "ollama":
        response_text = payload.get("response", "")
    else:
        choices = payload.get("choices")
        response_text = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            message_response = choice.get("message")
            if isinstance(message_response, dict):
                content = message_response.get("content", "")
                if isinstance(content, str):
                    response_text = content
                elif isinstance(content, list):
                    response_text = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
            if not response_text and isinstance(choice.get("text"), str):
                response_text = choice["text"]
        if not response_text:
            raise ValueError(
                "OpenAI-compatible LLM returned no completion text. "
                f"Response keys: {sorted(payload)}"
            )
    LOGGER.info(
        "%s completed classification for subject %r: model=%s done=%s "
        "done_reason=%s total_duration_ns=%s load_duration_ns=%s "
        "prompt_eval_count=%s prompt_eval_duration_ns=%s eval_count=%s "
        "eval_duration_ns=%s",
        provider,
        message["subject"],
        model,
        payload.get("done"),
        payload.get("done_reason"),
        payload.get("total_duration"),
        payload.get("load_duration"),
        payload.get("prompt_eval_count"),
        payload.get("prompt_eval_duration"),
        payload.get("eval_count"),
        payload.get("eval_duration"),
    )
    return parse_json_response(response_text)


def viewer_id(message: dict[str, Any]) -> str:
    return hashlib.sha256(message["message_id"].encode("utf-8")).hexdigest()


def save_original_email(message: dict[str, Any]) -> str:
    message_id = viewer_id(message)
    EMAILS_PATH.mkdir(parents=True, exist_ok=True)
    temporary_path = EMAILS_PATH / f"{message_id}.eml.tmp"
    email_path = EMAILS_PATH / f"{message_id}.eml"
    with temporary_path.open("wb") as email_file:
        email_file.write(message["raw"])
    temporary_path.replace(email_path)
    metadata_path = EMAILS_PATH / f"{message_id}.json"
    metadata_path.write_text(
        json.dumps(
            {
                "uid": message["uid"],
                "mailbox": str(message.get("mailbox", "")),
                "sender": message.get("sender", ""),
            }
        ),
        encoding="utf-8",
    )
    return message_id


def save_classification(message: dict[str, Any], classification: dict[str, str]) -> None:
    metadata_path = EMAILS_PATH / f"{viewer_id(message)}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(
        {
            "status": classification["status"],
            "summary": classification["summary"],
        }
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


async def publish_event(
    config: dict[str, Any],
    message: dict[str, str],
    classification: dict[str, str],
) -> None:
    event_data = {
        "message_id": message["message_id"],
        "sender": message["sender"],
        "subject": message["subject"],
        "body": message["body"],
        "status": classification["status"],
        "summary": classification["summary"],
        "viewer_id": viewer_id(message),
        "viewer_url": (
            str(config.get("viewer_base_url", "http://homeassistant.local:8099")).rstrip("/")
            + f"/email/{viewer_id(message)}"
        ),
    }
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    api_token = str(config.get("homeassistant_api_token", "")).strip()
    if supervisor_token:
        event_url = "http://supervisor/core/api/events/email_processed"
        authorization = "Bearer " + supervisor_token
    elif api_token:
        event_url = "http://homeassistant:8123/api/events/email_processed"
        authorization = "Bearer " + api_token
    else:
        raise RuntimeError(
            "No Home Assistant API token is available. Enter a long-lived "
            "access token in the AI Email app Configuration page."
        )
    await http_json(
        event_url,
        event_data,
        headers={"Authorization": authorization},
    )
async def run() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.get("log_level", "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    missing = [
        name
        for name in ("imap_host", "imap_username", "imap_password")
        if not str(config.get(name, "")).strip()
    ]
    if missing:
        raise RuntimeError(
            "Configure these required fields in the AI Email app Configuration page: "
            + ", ".join(missing)
        )
    provider = str(config.get("llm_provider", "ollama")).strip().lower()
    base_url = str(config.get("llm_base_url", "")).strip() or str(
        config.get("ollama_url", "")
    ).strip()
    model = str(config.get("llm_model", "")).strip() or str(
        config.get("ollama_model", "")
    ).strip()
    if provider not in {"ollama", "openai_compatible", "home_assistant"}:
        raise RuntimeError(
            f"Unsupported llm_provider {provider!r}; use ollama, openai_compatible, "
            "or home_assistant"
        )
    if provider != "home_assistant" and (not base_url or not model):
        raise RuntimeError(
            "Configure llm_base_url and llm_model in the AI Email app Configuration page"
        )
    state = load_state()
    processed_uids = set(state["processed_uids"])
    processed_message_ids = set(state["processed_message_ids"])
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    api_token = str(config.get("homeassistant_api_token", "")).strip()
    viewer_token = supervisor_token or api_token
    viewer_url = (
        "http://supervisor/core/api/events/email_deleted"
        if supervisor_token
        else "http://homeassistant:8123/api/events/email_deleted"
    )
    processed_event_url = (
        "http://supervisor/core/api/events/email_processed"
        if supervisor_token
        else "http://homeassistant:8123/api/events/email_processed"
    )
    viewer = start_viewer(
        EMAILS_PATH,
        event_config={
            "token": viewer_token or "",
            "url": viewer_url,
            "processed_url": processed_event_url,
            "viewer_base_url": str(config.get("viewer_base_url", "")),
            "llm_provider": provider,
            "llm_base_url": base_url,
            "llm_api_key": str(config.get("llm_api_key", "")),
            "imap_host": str(config.get("imap_host", "")),
            "imap_port": str(config.get("imap_port", 143)),
            "imap_security": str(config.get("imap_security", "starttls")),
            "smtp_host": str(config.get("smtp_host", "")),
            "smtp_port": str(config.get("smtp_port", 25)),
            "smtp_security": str(config.get("smtp_security", "none")),
            "smtp_use_tls": str(bool(config.get("smtp_use_tls", False))).lower(),
            "smtp_username": str(config.get("smtp_username", "")),
            "smtp_from": str(config.get("smtp_from", "")),
            "smtp_password": str(config.get("smtp_password", "")),
            "imap_username": str(config.get("imap_username", "")),
            "imap_host": str(config.get("imap_host", "")),
            "imap_port": str(config.get("imap_port", 143)),
            "imap_security": str(config.get("imap_security", "starttls")),
            "imap_verify_ssl": str(bool(config.get("imap_verify_ssl", False))).lower(),
            "imap_password": str(config.get("imap_password", "")),
            "imap_mailbox": str(config.get("imap_mailbox", "INBOX")),
            "settings_path": str(SETTINGS_PATH),
            "classification_criteria": str(config.get("classification_criteria", "")),
        },
        port=int(config.get("viewer_port", 8099)),
    )
    asyncio.get_running_loop().run_in_executor(None, viewer.serve_forever)
    LOGGER.info("Email viewer listening on port %s", config.get("viewer_port", 8099))
    poll_interval = int(config["poll_interval"])
    while True:
        try:
            messages = await asyncio.to_thread(
                fetch_unread, config, processed_uids, processed_message_ids
            )
            if messages:
                LOGGER.info("Found %s unread email(s) to process", len(messages))
            for message in messages:
                try:
                    save_original_email(message)
                    classification = await classify(config, message)
                    await publish_event(config, message, classification)
                    save_classification(message, classification)
                    processed_uids.add(message["uid"])
                    processed_message_ids.add(message["message_id"])
                    state["processed_uids"] = list(processed_uids)[-1000:]
                    state["processed_message_ids"] = list(processed_message_ids)[-1000:]
                    save_state(state)
                    LOGGER.info(
                        "Processed UID %s / Message-ID %s / %s as %s",
                        message["uid"],
                        message["message_id"],
                        message["subject"],
                        classification["status"],
                    )
                except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                    LOGGER.exception(
                        "Could not process UID %s / Message-ID %s; will retry",
                        message["uid"],
                        message["message_id"],
                    )
        except Exception:
            LOGGER.exception("Email polling cycle failed")
        await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    asyncio.run(run())
