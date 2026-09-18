# AI Email Home Assistant add-on

This is a Home Assistant add-on named **AI Email**. Once the repository is
added to Home Assistant, it appears under **Settings > Apps**.

[![Add this app repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhuseman21%2FAI-Email-for-HomeAssistant)

Click the button to open Home Assistant's **Add repository** dialog with this
repository URL prefilled, then confirm the dialog and install **AI Email** from
the app store.

## Development

The add-on polls an IMAP mailbox for unread messages, sends each new message to
the configured local LLM, and publishes an `email_processed` event
to Home Assistant. A UID state file in `/data` prevents the same message from
being classified twice after a restart. The add-on is built locally from the
included `Dockerfile`; it does not require a pre-published container image.

The add-on requests both Home Assistant API and Supervisor API access so the
runtime can publish `email_processed` events. If the injected
`SUPERVISOR_TOKEN` is unavailable, enter a Home Assistant long-lived access
token in the app Configuration page under `homeassistant_api_token`.

The add-on also runs a local email viewer on port `8099`. It preserves the
original RFC 822 message, renders HTML mail, serves inline CID images, and
provides a download link for the original message. The add-on exposes this
port locally and includes an **AI Email** ingress panel. Set
`viewer_base_url` to the Home Assistant hostname or IP that your browser uses
if `homeassistant.local` is not resolvable on your client devices.
Original messages are saved when they are processed by viewer-enabled version
1.1.1 or later. Links for messages processed before that version cannot show
the original email because their raw `.eml` content was not retained.

Deleting a message from the viewer now deletes the corresponding IMAP UID from
the configured mailbox and expunges it from the server before removing the
local viewer copy. Messages processed before version 1.4.0 do not have stored
IMAP metadata, so they can only be removed from the viewer after being
reprocessed.

Each message is saved to the add-on state file only after classification and
event publication succeed. A failed LLM request, invalid model response, or
Home Assistant API request is logged and retried on a later polling cycle.
Processing continues with other messages in the same cycle. Messages are
tracked by both IMAP UID and RFC Message-ID to avoid duplicate classifications.

The add-on can also be installed from a local Home Assistant add-on repository:

1. Place the `ai_email` folder inside a directory used for local add-ons, for
   example `/addons/ai_email`.
2. In Home Assistant, open **Settings > Apps**.
3. Open the three-dot menu, choose **Repositories**, and add the parent
   directory containing this folder.
4. Refresh the app store, open **AI Email**, and choose **Install**.
5. Open **Configuration** and provide the IMAP server, username, password,
   mailbox, Home Assistant API token, LLM provider, base URL, and model, then click
   **Save** before starting the app.
   The default `imap_host`, `imap_username`, and `imap_password` values are
   intentionally blank, so the app will stop until they are filled in.

Example configuration values:

```yaml
imap_host: mail.example.com
imap_port: 143
imap_security: starttls
imap_username: huseman@huseman.co
imap_password: your-mail-password-or-app-password
imap_mailbox: INBOX
smtp_host: mail.example.com
smtp_port: 25
smtp_security: none
smtp_use_tls: false
smtp_username: huseman@huseman.co
smtp_from: huseman@huseman.co
smtp_password: your-mail-password-or-app-password
homeassistant_api_token: your-home-assistant-long-lived-access-token
viewer_port: 8099
viewer_base_url: http://homeassistant.local:8099
llm_provider: ollama
llm_base_url: http://192.168.1.50:11434
llm_model: llama3.2
llm_api_key: ""
poll_interval: 60
```

Use an app-specific password when your mail provider supports or requires one.
The LLM URL must be reachable from the Home Assistant add-on container;
`localhost` refers to the container itself, not the Home Assistant host.

`llm_provider: ollama` uses Ollama's native `/api/generate` endpoint. To use
KoboldCpp or another server implementing the OpenAI-compatible API, set
`llm_provider: openai_compatible`, set `llm_base_url` to the server's `/v1`
URL, and set `llm_model` to the model name exposed by that server. For example:

```yaml
llm_provider: openai_compatible
llm_base_url: http://192.168.200.8:5001/v1
llm_model: your-koboldcpp-model
llm_api_key: ""
```

The optional API key is sent as a Bearer token when provided. Existing
configurations using `ollama_url` and `ollama_model` continue to work; the new
`llm_*` settings take precedence when they are filled in.

Create the token from your Home Assistant user profile under
**Long-Lived Access Tokens**, then paste it into the add-on's
`homeassistant_api_token` field. Do not commit this token to source control.

For an Outlook-compatible IMAP server using port 143, set
`imap_security: starttls`. Use `imap_security: ssl` with port 993 when the
server uses implicit TLS. The `imap_host` value must be the hostname or IP
address of your local mail server; it is not the email address or the LLM
address.

Replies use SMTP. Configure `smtp_host`, `smtp_port`, `smtp_security`,
`smtp_username`, and `smtp_password` in the app Configuration page. The
viewer uses the message's `Reply-To` header when available, otherwise its
`From` header, and sends a threaded `Re:` response. SMTP security options are
`starttls` (usually port 587), `ssl` (usually port 465), and `none`
(typically a trusted local relay on port 25). Set `smtp_use_tls: false` for a
local server that does not advertise STARTTLS. Set it to `true` when the
server supports TLS; `smtp_security` then selects STARTTLS or implicit SSL.
SMTP authentication is used automatically when both `smtp_username` and
`smtp_password` are provided.

`ConnectionRefusedError` means the add-on reached the target address but no
service accepted connections on that port. It occurs before username,
password, or STARTTLS are checked. Confirm that the local mail server is
listening on port 143, its firewall allows connections from the Home
Assistant host, and `imap_host` is the server's LAN IP or DNS name. Do not
use `localhost` or `127.0.0.1` unless the IMAP server runs inside this same
add-on container. If the server only exposes implicit TLS, use port 993 with
`imap_security: ssl`.

For a local server with a self-signed certificate, leave
`imap_verify_ssl: false` (the default). This accepts the server certificate
for both STARTTLS and implicit SSL connections. Set it to `true` only after
installing a certificate signed by a trusted certificate authority or adding
the server's CA certificate to the container.

## Home Assistant configuration

Add [homeassistant/template_sensors.yaml](<homeassistant/template_sensors.yaml>)
to the Home Assistant configuration directory and include it from
`configuration.yaml`:

```yaml
template: !include homeassistant/template_sensors.yaml
```

If the file is included this way, remove the top-level `template:` line from
the included file so that only its list entries remain. Alternatively,
copy the file's two list entries directly under the existing `template:`
section. Restart Home Assistant after changing the configuration.

The included [homeassistant/lovelace.yaml](<homeassistant/lovelace.yaml>) is a
dashboard view that can be copied into a YAML dashboard or adapted into the
dashboard's raw configuration editor. It displays category counts, the latest
processing timestamp, and the latest ten messages in each category.

To add it as a YAML dashboard, copy the file to the Home Assistant
configuration directory, then add this to `configuration.yaml`:

```yaml
lovelace:
  dashboards:
    ai-email:
      mode: yaml
      title: AI Email
      icon: mdi:email-check-outline
      filename: lovelace-ai-email.yaml
```

Use `lovelace-ai-email.yaml` as the filename when copying the provided
dashboard file, restart Home Assistant, and select **AI Email** from the
sidebar.

## Files

- `ai_email/config.yaml` - Home Assistant add-on metadata, options, and supported
  architectures.
- `ai_email/build.yaml` - Architecture-specific Home Assistant base images used to build
  the add-on locally.
- `ai_email/Dockerfile` - Container image definition.
- `ai_email/run.sh` - Legacy container entrypoint retained for compatibility.
- `ai_email/requirements.txt` - Python runtime dependency.
- `ai_email/app/main.py` - IMAP polling, LLM classification, state persistence, and
  Home Assistant event publishing.
- `homeassistant/template_sensors.yaml` - Trigger-based email sensors.
- `homeassistant/lovelace.yaml` - Two-column dashboard view.
