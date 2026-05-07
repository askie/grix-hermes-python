# grix-hermes

Standard Grix platform plugin for Hermes Agent.

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
hermes plugins install <git-url-or-owner/repo> --enable
```

If you use a Hermes profile, set `HERMES_HOME` to that profile first:

```bash
export HERMES_HOME=/Users/you/.hermes/profiles/<profile-name>
hermes plugins install <git-url-or-owner/repo> --enable
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
