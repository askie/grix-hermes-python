"""Unit tests for Hermes queue -> progress-card rendering."""

import urllib.parse

import pytest

from grix_hermes.card_links import build_progress_card
from grix_hermes.progress_cards import build_queue_progress_card


def _params(link):
    assert link.startswith("[")
    uri = link[link.index("](") + 2 : -1]
    assert uri.startswith("grix://card/progress?")
    return dict(urllib.parse.parse_qsl(uri.split("?", 1)[1]))


def test_queue_message_with_iteration_renders_percent():
    link = build_queue_progress_card("⏳ Queued for the next turn (iteration 4/8). ")
    assert link is not None
    params = _params(link)
    assert params["label"] == "Working · iteration 4/8"
    assert params["percent"] == "50"


def test_queue_message_without_iteration_is_indeterminate():
    link = build_queue_progress_card("⏳ Queued for the next turn.")
    assert link is not None
    params = _params(link)
    assert "percent" not in params


def test_working_message_renders_progress_card():
    link = build_queue_progress_card("⏳ Working — 3 min — iteration 9/90, process")
    assert link is not None
    params = _params(link)
    assert params["label"] == "Working · iteration 9/90 · 3 min elapsed"
    assert params["percent"] == "10"


def test_working_without_iteration():
    link = build_queue_progress_card("⏳ Working — 1 min")
    assert link is not None
    params = _params(link)
    assert params["label"] == "Working · 1 min elapsed"
    assert "percent" not in params


def test_non_queue_status_returns_none():
    assert build_queue_progress_card("⏳ Still working... (iteration 16/90)") is None
    assert build_queue_progress_card("⚠️ No activity for 15 min.") is None
    assert build_queue_progress_card("") is None


def test_build_progress_card_encodes_label_and_percent():
    params = _params(build_progress_card("正在编译", 30))
    assert params["label"] == "正在编译"
    assert params["percent"] == "30"


def test_build_progress_card_clamps_percent():
    assert _params(build_progress_card("x", 150))["percent"] == "100"
    assert _params(build_progress_card("x", -5))["percent"] == "0"


def test_build_progress_card_requires_label():
    with pytest.raises(ValueError):
        build_progress_card("")
