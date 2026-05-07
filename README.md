# grix-hermes

`grix-hermes` is a standard Hermes platform plugin that connects a Hermes gateway
to Grix through the aibot/Grix Agent API protocol.

## What this plugin provides

- A Hermes messaging platform plugin named `grix-hermes`
- Tools for Grix message, group, auth, and agent operations
- Plugin-scoped skills for profile bootstrap and group operations

## Requirements

You need a Grix API Agent and these three credentials:

- `GRIX_ENDPOINT` - WebSocket endpoint
- `GRIX_AGENT_ID` - agent ID
- `GRIX_API_KEY` - API key

You can create an API Agent from [grix.dhf.pub](https://grix.dhf.pub/) in the
`AI` section.

## Choose the target Hermes home

Install and configure the plugin in the same Hermes home that will run the
gateway.

Common cases:

- Global Hermes home: `~/.hermes`
- Profile Hermes home: `~/.hermes/profiles/<profile-name>`

If you use a profile-specific gateway, set `HERMES_HOME` to that profile before
installing or restarting anything. Installing the plugin only into the global
`~/.hermes` does not make it available inside another profile's Hermes home.

Example:

```bash
export HERMES_HOME=/Users/you/.hermes/profiles/grix-online
```

## Standard installation

This is the recommended path.

Install from a Git URL or repository reference:

```bash
hermes plugins install <git-url-or-owner/repo> --enable
```

Install from a local checkout:

```bash
hermes plugins install file:///absolute/path/to/grix-hermes --enable
```

Notes:

- `--enable` updates the target Hermes config to enable `grix-hermes`
- The plugin is installed into `<HERMES_HOME>/plugins/grix-hermes`
- When `HERMES_HOME` is unset, Hermes uses the default global home

## PyPI package

You can also install the Python package:

```bash
pip install grix-hermes
```

This installs the Python package only. It does not, by itself, install and
enable the plugin inside a Hermes home. For normal Hermes usage, prefer
`hermes plugins install`.

## Configure credentials

Write the Grix credentials into the target Hermes home's `.env` file:

- Global home: `~/.hermes/.env`
- Profile home: `~/.hermes/profiles/<profile-name>/.env`

Example:

```bash
cat >> "$HERMES_HOME/.env" <<'EOF'
GRIX_ENDPOINT=wss://your-grix-endpoint
GRIX_AGENT_ID=your-agent-id
GRIX_API_KEY=your-api-key
EOF
```

Optional access controls:

- `GRIX_ALLOW_ALL_USERS=true`
- `GATEWAY_ALLOW_ALL_USERS=true`
- `GRIX_ALLOWED_USERS=*`

Use the access model that matches your environment and policy.

## Gateway service setup

After plugin install or Hermes path changes, refresh the gateway service
definition for the target Hermes home:

```bash
hermes gateway install --force
hermes gateway restart
```

Why this matters:

- Hermes may run the gateway through a background service such as `launchd`
- If that service still points to an old Hermes install path, the gateway can
  fail even when the plugin and credentials are correct

## Validate the installation

Check service status:

```bash
hermes gateway status
```

Check recent logs:

```bash
tail -n 50 "$HERMES_HOME/logs/gateway.log"
```

Successful startup should show lines like:

- `Connecting to grix...`
- `grix connected`
- `Gateway running with 1 platform(s)`

You can also confirm that the plugin is installed under:

- `<HERMES_HOME>/plugins/grix-hermes`

and enabled in:

- `<HERMES_HOME>/config.yaml`

## Common problems

### Plugin installed globally but not visible in a profile

Symptom:

- Hermes starts, but the profile does not discover `grix-hermes`

Cause:

- The plugin was installed into `~/.hermes`, but the gateway runs from
  `~/.hermes/profiles/<profile-name>`

Fix:

- Set `HERMES_HOME` to the target profile
- Re-run `hermes plugins install ... --enable`

### Gateway service points to an old Hermes path

Symptom:

- `hermes gateway status` shows the service is loaded
- The gateway never becomes healthy
- macOS service metadata or logs refer to a path that no longer exists

Fix:

```bash
hermes gateway install --force
hermes gateway restart
```

### Grix authentication failed

Symptom in `gateway.log`:

- `grix auth failed: code=10001 msg=auth failed`

Cause:

- The current `GRIX_ENDPOINT`, `GRIX_AGENT_ID`, and `GRIX_API_KEY` do not match
  a valid Grix API Agent

Fix:

- Re-check the three `GRIX_*` values
- Rotate or recreate the API Agent if needed

### Gateway says no platforms are enabled

Symptom:

- Logs mention that no messaging platforms are enabled

Fix:

- Make sure `grix-hermes` is present in `<HERMES_HOME>/config.yaml`
- Re-run the plugin install with `--enable`

## Provided tools

| Tool | Description |
|------|-------------|
| `grix_invoke` | WS-based operations: send/delete messages, query contacts/sessions, manage groups, and admin agents |
| `grix_auth` | HTTP auth: send email code, register, login, and create or rotate agent API keys |
| `grix_card` | Generate Grix deep-link cards for conversations, profiles, and install status |
| `grix_egg` | Agent bootstrap flow: detect -> install -> create -> bind -> soul -> gateway -> accept |

## Plugin skills

After installation, Hermes can load these namespaced skills:

- `grix-hermes:group-ops` - group creation, member management, mute controls, and speaking permissions
- `grix-hermes:agent-bootstrap` - install, bind, enable, and verify a Grix-backed Hermes profile

## Recommended install checklist

1. Choose the exact Hermes home or profile that will run the gateway.
2. Install the plugin with `hermes plugins install ... --enable`.
3. Write `GRIX_ENDPOINT`, `GRIX_AGENT_ID`, and `GRIX_API_KEY` into that target
   Hermes home's `.env`.
4. Run `hermes gateway install --force`.
5. Run `hermes gateway restart`.
6. Verify `hermes gateway status` and `gateway.log` until you see
   `grix connected`.

## License

MIT
