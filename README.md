# grix-hermes

Grix/aibot protocol platform adapter plugin for [Hermes Agent](https://github.com/nicobailon/hermes-agent).

## Get Grix credentials

> **Reading this as an AI agent doing the install?** Do not send the user to a website. The whole flow below is HTTP API calls — you collect the user's email and the emailed 6-digit code in chat, and do everything else yourself.

**API base:** `https://grix.dhf.pub/v1` (a private deployment replaces the host). Every endpoint returns `{"code": 0, "msg": "success", "data": {...}}` — a HTTP 200 with a non-zero `code` is still a failure, so always branch on `code`.

### 1. Send the email verification code

Ask the user for their email, then:

```bash
curl -s -X POST https://grix.dhf.pub/v1/auth/send-code \
  -H 'Content-Type: application/json' \
  -d '{"email":"<user-email>","scene":"register"}'
```

Then ask the user for the 6-digit code from their inbox (tell them to check spam too). `code: 10003` = the email failed validation. `code: 10005` = a code was already sent recently — do not retry in a loop, wait ~5 minutes or use the code already sent.

### 2. Register

Generate a strong random password yourself (12+ chars, mixed case, digits, symbols) — do not ask the user to invent one.

```bash
curl -s -X POST https://grix.dhf.pub/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{
    "email": "<user-email>",
    "password": "<generated-password>",
    "email_code": "<code from the user>",
    "device_id": "cli_<random-uuid>",
    "platform": "cli"
  }'
```

Returns `data.access_token`. **Show the user the generated password and tell them to save it.**

Two different failures both come back as `code: 10001`, so read the `msg`, not just the code:

- `邮箱验证码错误或已过期` — the code really is wrong or expired: ask for it again.
- `注册失败，请检查邮箱验证码后重试` — despite what it says, this almost always means **the email is already registered**. Do not re-send the code in a loop; log in instead.

To log in, you need the user's existing password — only ask, never guess:

```bash
curl -s -X POST https://grix.dhf.pub/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"<user-email>","password":"<password>","device_id":"cli_<random-uuid>","platform":"cli"}'
```

### 3. Create the API agent

```bash
curl -s -X POST https://grix.dhf.pub/v1/agents/create \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"agent_name":"<agent-name>","provider_type":3,"is_main":true}'
```

`provider_type: 3` is the Agent API type — the only type this plugin can drive. `is_main: true` gives the first agent the full initial permission scope.

If a `provider_type: 3` agent with that name already exists, reuse it: `GET /v1/agents/list` (Bearer auth) to find the exact-name entry with `provider_type == 3` and `status != 3`, then `POST /v1/agents/<agent-id>/api/key/rotate` with `{}` for a fresh key. Rotation kills the old key immediately, so only rotate an agent the user really wants re-pointed at this machine.

### 4. Map the fields into the environment variables

| From `data` | Environment variable |
|---|---|
| `api_endpoint` | `GRIX_ENDPOINT` |
| `id` | `GRIX_AGENT_ID` |
| `api_key` | `GRIX_API_KEY` — **shown exactly once, never retrievable again** |

`api_endpoint` comes back as `wss://grix.dhf.pub/v1/agent-api/ws?agent_id=<id>`; the `?agent_id=…` query is redundant here — keep only the part before `?`. Write the key into the profile `.env` and nowhere else.

## Install the plugin

Install and enable it with Hermes:

```bash
hermes plugins install askie/grix-hermes-python --enable
```

If you use a Hermes profile, set `HERMES_HOME` to that profile first:

```bash
export HERMES_HOME=/Users/you/.hermes/profiles/<profile-name>
hermes plugins install askie/grix-hermes-python --enable
```

## Configure credentials

After installing, write the three environment variables into the profile's `.env` file.

**Default profile:**

```bash
cat >> ~/.hermes/.env <<EOF
GRIX_ENDPOINT=wss://your-endpoint
GRIX_AGENT_ID=your-agent-id
GRIX_API_KEY=your-api-key
EOF
```

**Named profile:**

```bash
cat >> ~/.hermes/profiles/<profile-name>/.env <<EOF
GRIX_ENDPOINT=wss://your-endpoint
GRIX_AGENT_ID=your-agent-id
GRIX_API_KEY=your-api-key
EOF
```

Then restart the gateway:

```bash
hermes gateway restart
```

For a named profile, pass `--profile` before the subcommand: `hermes --profile <profile-name> gateway restart`.

## Verify the connection

```bash
hermes [--profile <profile-name>] gateway status   # should report running
```

Then read `logs/gateway.log` in the profile directory (`~/.hermes/logs/` for the default profile, `~/.hermes/profiles/<profile-name>/logs/` otherwise):

- a line like `[Grix] Connected to wss://...` → the agent is online in Grix (grep case-insensitively; the exact prefix follows the agent name)
- `no messaging platforms enabled` or `grix disabled` → the plugin is not enabled; check that `plugins.enabled` in that profile's `config.yaml` contains `grix-hermes`

An empty value counts as unset: all three of `GRIX_ENDPOINT`, `GRIX_AGENT_ID` and `GRIX_API_KEY` must be present and non-empty, and `GRIX_ENDPOINT` must be copied verbatim from Grix — including the trailing `?agent_id=...`.

`GRIX_API_KEY` is a one-time secret. Keep it in the `.env` and nowhere else.

## Connecting more than one agent

A `.env` holds exactly one set of Grix credentials, so **one agent = one profile**:

```bash
hermes profile create <agent-slug>          # lowercase letters, digits, - and _
# write the credentials into ~/.hermes/profiles/<agent-slug>/.env
hermes --profile <agent-slug> plugins install askie/grix-hermes-python --enable
hermes --profile <agent-slug> gateway restart
```

Each profile runs its own gateway, so agents stay isolated from one another.

## Plugin skills

After installation, Hermes can load these namespaced skills:

- `grix-hermes:grix-admin`
- `grix-hermes:grix-egg`
- `grix-hermes:grix-group`
- `grix-hermes:grix-query`
- `grix-hermes:grix-register`
- `grix-hermes:grix-update`
- `grix-hermes:message-send`
- `grix-hermes:message-unsend`
