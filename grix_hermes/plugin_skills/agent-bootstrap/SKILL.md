---
name: agent-bootstrap
description: Bootstrap or bind a Grix-backed Hermes agent profile, enable the grix-hermes plugin, write connection settings, and validate gateway startup.
---

Use this skill when the task is to install or wire a Hermes profile to Grix.

Preferred tools:
- `grix_egg`
- `grix_auth`
- `grix_invoke`

What this skill covers:
- create or reuse a Grix API Agent
- bind `GRIX_ENDPOINT`, `GRIX_AGENT_ID`, and `GRIX_API_KEY`
- enable the `grix-hermes` plugin in profile config
- start or restart the Hermes gateway
- confirm the profile reaches `grix connected`

Working rules:
- Prefer the plugin's bootstrap flow over ad hoc file edits.
- Keep the plugin name consistent as `grix-hermes`.
- If the target profile already exists, preserve unrelated settings.
- After install or bind, verify both plugin discovery and gateway connection.

Success checklist:
1. Plugin exists under the target Hermes `plugins/grix-hermes` path.
2. The target config enables `grix-hermes`.
3. The target `.env` has complete `GRIX_*` credentials.
4. Gateway status is running and logs show `grix connected`.
