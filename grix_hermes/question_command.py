"""Parse the server's legacy ``/grix question`` command back into an answer.

The aibot server rewrites card-tap replies (``grix://card/agent_question_reply
?d=...``) into a legacy command line before delivering to hermes-family agents
(backend/internal/agentadapter/hermes/adapter.go ``NormalizeOutbound`` →
``grixactions.RewriteToLegacyCommand``), matching the strict public
aibot-agent-api-v1 profile. The wire formats (grixactions.LegacyQuestionCommand):

  single response:   /grix question <request_id> <value>
  map response:      /grix question <request_id> k=v; k2=v2
  accept action:     /grix question <request_id> __grix_accept__
  cancel action:     /grix question <request_id> __grix_cancel__

Without a handler, hermes-agent treats the line as an unknown slash command
and swallows it — the tapped answer never reaches the pending clarify and the
agent blocks until the clarify timeout. This module recovers
``(request_id, answer_text)`` so the adapter can resolve the clarify directly.
"""

from typing import Optional, Tuple

_COMMAND_PREFIX = "/grix question"
_ACCEPT_TOKEN = "__grix_accept__"
_CANCEL_TOKEN = "__grix_cancel__"


def parse_grix_question_command(text: str) -> Optional[Tuple[str, str]]:
    """Return ``(request_id, answer_text)`` for a ``/grix question`` line.

    Returns ``None`` for anything else — including other ``/grix`` verbs,
    a missing request_id, or an empty answer — so callers can fall through
    to normal message handling.
    """
    stripped = str(text or "").strip()
    if not stripped.startswith(_COMMAND_PREFIX):
        return None
    rest = stripped[len(_COMMAND_PREFIX):]
    # Guard against e.g. "/grix questionnaire ...".
    if rest and not rest[0].isspace():
        return None

    parts = rest.strip().split(None, 1)
    if len(parts) < 2:
        return None

    request_id = parts[0].strip()
    answer = parts[1].strip()
    if not request_id or not answer:
        return None

    if answer == _ACCEPT_TOKEN:
        answer = "accept"
    elif answer == _CANCEL_TOKEN:
        answer = "cancel"
    return request_id, answer
