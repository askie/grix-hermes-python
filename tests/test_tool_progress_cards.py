"""Tool-progress detection across both hermes progress shapes.

Friendly labels (hermes #55166) are the default for built-in tools; custom /
plugin / MCP tools still emit the raw ``tool_name: "preview"`` form.  Both must
resolve to a (tool_name, preview) pair so the adapter emits a tool_execution
card instead of leaking the line as chat text.
"""

from grix_hermes.tool_progress_cards import (
    build_tool_execution_channel_data,
    detect_tool_progress,
)


# --- friendly labels (built-in tools) --------------------------------------

def test_friendly_read_file():
    assert detect_tool_progress("📖 Reading docs/api.md") == ("read_file", "docs/api.md")


def test_friendly_patch():
    assert detect_tool_progress("✏️ Editing grix_hermes/adapter.py") == (
        "patch",
        "grix_hermes/adapter.py",
    )


def test_friendly_terminal():
    assert detect_tool_progress("💻 Running ls -la") == ("terminal", "ls -la")


def test_friendly_web_search_uses_for_connector():
    assert detect_tool_progress("🔍 Searching the web for grix hermes") == (
        "web_search",
        "grix hermes",
    )


def test_friendly_search_files_uses_for_connector():
    assert detect_tool_progress("🔎 Searching files for detect_tool") == (
        "search_files",
        "detect_tool",
    )


def test_friendly_longest_verb_wins_over_prefix_overlap():
    # "Running code" must not be parsed as terminal ("Running" + "code ...").
    assert detect_tool_progress("🐍 Running code print(1)") == ("execute_code", "print(1)")


def test_friendly_reading_url_is_web_extract():
    # "Reading" is shared by read_file and web_extract; a URL preview means the web tool.
    assert detect_tool_progress("🌐 Reading https://example.com/a") == (
        "web_extract",
        "https://example.com/a",
    )


def test_friendly_reading_skill_beats_reading():
    assert detect_tool_progress("📖 Reading skill grix-hermes-release") == (
        "skill_view",
        "grix-hermes-release",
    )


def test_friendly_verb_without_preview():
    assert detect_tool_progress("📋 Listing skills") == ("skills_list", "")


def test_friendly_todo():
    assert detect_tool_progress("📋 Updating tasks step 2/5") == ("todo", "step 2/5")


def test_friendly_with_dedup_counter():
    assert detect_tool_progress("📖 Reading docs/api.md (×3)") == ("read_file", "docs/api.md")


def test_friendly_truncated_preview_kept_verbatim():
    assert detect_tool_progress("💻 Running git log --oneline --grap...") == (
        "terminal",
        "git log --oneline --grap...",
    )


# --- raw form (MCP / plugin tools, or friendly labels disabled) ------------

def test_raw_preview_form():
    assert detect_tool_progress('🔍 web_search: "grix hermes"') == ("web_search", "grix hermes")


def test_raw_mcp_tool():
    assert detect_tool_progress('⚙️ grix_reply: "done"') == ("grix_reply", "done")


def test_raw_no_preview_form():
    assert detect_tool_progress("⚙️ memory...") == ("memory", "")


def test_raw_verbose_form():
    content = '⚙️ read_file([\'path\'])\n{"path": "docs/api.md"}'
    assert detect_tool_progress(content) == ("read_file", "['path']")


# --- accumulated multi-line bubbles ----------------------------------------

def test_multiline_reports_last_tool():
    content = "📖 Reading a.py\n✏️ Editing b.py\n💻 Running pytest"
    assert detect_tool_progress(content) == ("terminal", "pytest")


def test_multiline_mixed_shapes():
    content = '📖 Reading a.py\n⚙️ grix_reply: "ok"'
    assert detect_tool_progress(content) == ("grix_reply", "ok")


# --- non-tool content must not be misread ----------------------------------

def test_plain_text_is_not_tool_progress():
    assert detect_tool_progress("我把改动合并到 main 了，测试全绿。") is None


def test_gateway_status_line_is_not_tool_progress():
    assert detect_tool_progress("⏳ Still working... (3 min elapsed)") is None


def test_no_activity_warning_is_not_tool_progress():
    assert detect_tool_progress("⚠️ No activity for 15 min.") is None


def test_empty_content():
    assert detect_tool_progress("") is None


def test_unknown_verb_is_not_tool_progress():
    assert detect_tool_progress("✅ Deployed to production") is None


# --- channel_data ----------------------------------------------------------

def test_channel_data_summary():
    cd = build_tool_execution_channel_data("read_file", "docs/api.md")
    assert cd["grix"]["toolExecution"]["summary_text"] == "read_file: docs/api.md"


def test_channel_data_summary_without_preview():
    cd = build_tool_execution_channel_data("skills_list", "")
    assert cd["grix"]["toolExecution"]["summary_text"] == "skills_list"
