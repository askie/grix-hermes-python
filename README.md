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
