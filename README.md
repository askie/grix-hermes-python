# grix-hermes

Grix/aibot protocol platform adapter plugin for [Hermes Agent](https://github.com/nicobailon/hermes-agent).

## Get Grix credentials

Open [grix.dhf.pub](https://grix.dhf.pub/), go to the `AI` section, and create an
`API Agent`.

You will get:

- `GRIX_ENDPOINT`
- `GRIX_AGENT_ID`
- `GRIX_API_KEY`

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
