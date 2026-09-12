---
name: grix-scheduled-trigger
description: Set up human- or timer-driven triggers for this agent — anything that must fire on a schedule, repeat on an interval, or be kicked off later by a person — by reusing (grix_invoke's webhook_list action) or creating (webhook_create) the session's standing webhook and registering a local scheduler job (macOS launchd/cron, Linux cron/systemd timer, Windows Task Scheduler) that POSTs to it; webhook_delete tears it down. Not for agent-to-agent dispatch callbacks.
trigger: 当用户要这个 agent 按计划运行、周期性重复、轮询某个状态，或由定时器/人而不是另一个 agent 在稍后唤醒时
---

# Grix Scheduled Trigger

Your process only runs while handling a chat turn. You cannot sleep, loop, or
wait in the background and "come back later". The reliable way to be woken at
a given time is a **session webhook** plus an **OS-level scheduler job**:

1. The session has one standing webhook URL (`grix_invoke(action="webhook_list", ...)`
   finds it, `grix_invoke(action="webhook_create", ...)` makes it if missing).
2. Any HTTP client POSTing `{"content": "..."}` to that URL sends the text into
   the session **as the owner** — exactly like the owner typing it — so you are
   triggered and receive it as a normal message.
3. A scheduler on this machine (launchd/cron/systemd timer/Task Scheduler) does
   the POST at the right times. The scheduler survives reboots and does not
   depend on your process being alive.

## When to use / not use

Use this mechanism whenever the trigger comes from **a timer or a human**:

- "every morning at 8 summarise X", "check the deploy every 10 minutes", "remind
  me in 2 hours", "run the daily report", polling an external state, recurring
  health checks, any one-shot or repeating wake-up.
- Anything a person wants to fire manually later (they can `curl` the URL or
  wire it into their own tooling).

Do **not** use it for **agent-driven** callbacks: dispatch results
(`report_dispatch_result`), replies to another agent, or anything already
covered by `session_send` / `dispatch_agent` (both `grix_invoke` actions).
Those flows have their own contract; a webhook there is redundant and leaks a
secret URL for nothing.

## Step 1 — reuse the session's standing webhook (create only if missing)

**One webhook per session, shared by every schedule targeting that session.**
Never create a second one while an active one exists.

1. `grix_invoke(action="webhook_list", params={"session_id": "<SESSION_ID>"})`
   with `session_id` = the **current** `chat_id` from the channel metadata. It
   returns the session's endpoints with full `url`, `status`, `expires_at`,
   `last_used_at`.
2. If there is an entry with `status: "active"` and no `expires_at`, use its
   `url`. If several exist, use the newest and
   `grix_invoke(action="webhook_delete", params={"id": "<ID>"})` the rest.
3. Only if none is usable:
   `grix_invoke(action="webhook_create", params={"session_id": "<SESSION_ID>"})`
   — same `session_id`, and **no `expires_at`** — the standing endpoint must
   not expire; one-shot reminders are made one-shot by the scheduler job
   (Step 3), not by the webhook. The result carries the `url`.

Rules:

- You may only list/create/delete webhooks for sessions you are a member of;
  the platform checks that and that the owner is a member too.
- Permission is granted per agent by the owner ("Create Session Webhook"). A
  permission error means it is not enabled — ask the owner to enable it in the
  agent's permission settings in the App. Do not retry.
- The platform caps active endpoints at 20 per session and rejects
  `expires_at` in the past; if you ever hit "too many active webhooks", list
  and delete duplicates instead of creating more.

Local registry: `~/.grix/scheduled/<session_id>.json` (directory mode 700,
file mode 600) stores `session_id`, the webhook `id` + `url`, and the list of
scheduler jobs you registered. If the file is lost, `webhook_list` recovers
the URL; the jobs are recovered from the scheduler itself.

## Step 2 — the trigger script

Write a small script per job under `~/.grix/scheduled/jobs/` that POSTs to the
URL. Use your normal shell-command execution tool to write and run it. Keep
the URL in the script (mode 600), never in the scheduler's visible arguments
or in chat.

Payload: JSON `{"content": "<message>", "msg_type": "text", "client_msg_id": "<unique>"}`.
`content` is what you will receive as the owner's message — make it
self-describing so a future turn knows what to do without context, e.g.
`[scheduled] daily-report 08:00 — generate yesterday's sales summary`.
`client_msg_id` must be unique per firing (job name + timestamp) so a retry
never posts twice.

**Group sessions**: the message is fanned out to every agent in the group and
each host applies its own group policy, so start `content` with an @-mention
of yourself (your own agent display name) to make sure this agent — and only
this agent — is woken. In a private owner↔agent session no mention is needed.

Reliability rules for the script:

- Retry on network failure / HTTP 5xx / 429 with backoff (e.g. 3 attempts,
  5s → 20s → 60s). Rate limit is 60 requests per minute per endpoint+IP.
- Treat 404 (`WEBHOOK_NOT_FOUND`), 410 (`WEBHOOK_EXPIRED`), 403
  (`WEBHOOK_FORBIDDEN`) as **permanent**: do not retry, log it, and on the next
  turn tell the owner the schedule is broken and needs a new webhook.
- Append one line per attempt to `~/.grix/scheduled/logs/<job>.log`
  (timestamp, HTTP status, message_id) so failures are diagnosable.

Minimal POSIX example (`~/.grix/scheduled/jobs/daily-report.sh`):

```sh
#!/bin/sh
URL='https://example.com/v1/webhook/incoming/whk_...'
JOB='daily-report'
LOG="$HOME/.grix/scheduled/logs/$JOB.log"
mkdir -p "$(dirname "$LOG")"
for delay in 5 20 60 0; do
  code=$(curl -sS -o /tmp/grix-$JOB.out -w '%{http_code}' -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -d "{\"content\":\"[scheduled] $JOB $(date '+%F %T') — generate yesterday's sales summary\",\"msg_type\":\"text\",\"client_msg_id\":\"$JOB-$(date +%s)\"}")
  echo "$(date '+%F %T') $code $(cat /tmp/grix-$JOB.out)" >> "$LOG"
  case "$code" in
    200) exit 0 ;;
    403|404|410) exit 1 ;;   # permanent — needs a new webhook
  esac
  [ "$delay" = 0 ] || sleep "$delay"
done
exit 1
```

On Windows write the equivalent as PowerShell (`Invoke-RestMethod`, same
retry/permanent-error rules).

## Step 3 — register the scheduler job

Pick the native scheduler for this OS; register with the user's own account
(no sudo):

- **macOS**: prefer `launchd` — write
  `~/Library/LaunchAgents/im.grix.scheduled.<job>.plist` with
  `StartCalendarInterval` (fixed times) or `StartInterval` (every N seconds),
  `ProgramArguments` = the script, then
  `launchctl bootstrap gui/$(id -u) <plist>` (fallback `launchctl load`).
  `crontab -e` also works but launchd runs missed jobs after sleep; cron does
  not.
- **Linux**: `crontab` entry (`0 8 * * * $HOME/.grix/scheduled/jobs/daily-report.sh`)
  or a user `systemd` timer (`systemctl --user enable --now <job>.timer`;
  add `Persistent=true` to catch up missed runs).
- **Windows**: `schtasks /Create /SC DAILY /ST 08:00 /TN "Grix\<job>" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File <script.ps1>"`
  (use `/SC MINUTE /MO N` for intervals). Run as the current user, no elevation.

One-shot reminders: reuse the standing webhook and make the **job** one-shot —
`launchd` `StartCalendarInterval` with a full date, `at` /
`systemd-run --user --on-calendar`, or `schtasks /SC ONCE` — and have the
script remove its own scheduler entry and script file after a successful 200.
Do not create a separate expiring webhook for it.

## Step 4 — verify before you report done

1. Run the script once by hand and confirm HTTP 200 **and** that the message
   actually arrived in this session (you will see it on your next turn, or
   check `grix_invoke(action="message_history", ...)` — see `grix-query`).
2. Confirm the scheduler accepted the job (`launchctl print gui/$(id -u)/<label>`,
   `crontab -l`, `systemctl --user list-timers`, `schtasks /Query /TN`).
3. Record the job in the registry file. Report to the owner: what fires, when,
   the log path, and how to remove it. Never paste the webhook URL into chat.

## Removing schedules

- Removing one job: unload/delete its scheduler entry, delete the script,
  update the registry. Keep the webhook — other jobs (or future ones) reuse it.
- Removing the session's scheduling entirely (owner asks to "stop all timers
  here"): remove every job, then
  `grix_invoke(action="webhook_delete", params={"id": "<ID>"})` for the
  endpoint `id` from the registry (or from `webhook_list`) so the URL stops
  working, and delete the registry file.
