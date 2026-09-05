"""Lock the grix-connector-bootstrap skill: registration + frontmatter + facts.

The skill tells a Hermes-only machine how to install grix-connector and bring up
its first agent. Every path/port/command it names is verified against the
grix-connector repo, so the test pins the ones that would silently break the
bootstrap if they drifted.
"""

import re
from pathlib import Path

from grix_hermes import PLUGIN_SKILLS

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "grix_hermes" / "plugin_skills" / "grix-connector-bootstrap"
SKILL_MD = SKILL_DIR / "SKILL.md"


def test_registered_in_default_plugin_skills():
    assert "grix-connector-bootstrap" in PLUGIN_SKILLS
    entry = PLUGIN_SKILLS["grix-connector-bootstrap"]
    assert entry["tools"] == ["grix_invoke"]
    assert "grix-connector" in entry["description"]


def test_skill_file_exists_with_valid_frontmatter():
    assert SKILL_MD.exists()
    text = SKILL_MD.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    end = text.index("\n---\n", 3)
    frontmatter = text[4:end]

    name = re.search(r"^name:\s*(\S+)", frontmatter, re.M)
    assert name and name.group(1) == "grix-connector-bootstrap"
    assert re.search(r"^description:\s*\S", frontmatter, re.M)
    assert re.search(r"^trigger:\s*\S", frontmatter, re.M)


def test_trigger_covers_the_agreed_phrases():
    trigger = SKILL_MD.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    for phrase in ["安装连接器", "grix-connector-bootstrap", "装 Grix 连接器",
                   "这台机器没有连接器"]:
        assert phrase in trigger


def test_documents_verified_connector_facts():
    text = SKILL_MD.read_text(encoding="utf-8")
    # install + runtime requirement
    assert "npm install -g grix-connector" in text
    assert "registry.npmmirror.com" in text
    assert "Node.js >= 18" in text
    # config location and required entry fields
    assert "~/.grix/config/agents.json" in text
    for field in ["ws_url", "agent_id", "api_key", "client_type"]:
        assert field in text
    assert "wss://grix.dhf.pub/v1/agent-api/ws" in text
    assert "wss://ws.grix.im/v1/agent-api/ws" in text
    # agent creation goes through grix-admin / grix_invoke
    assert 'grix_invoke(action="agent_api_create"' in text
    assert '"provider_type": 3' in text
    # start + health verification
    assert "grix-connector start" in text
    assert "http://127.0.0.1:19579/healthz" in text
    assert "http://127.0.0.1:19580/api/agents" in text
    assert "wsConnected" in text


def test_states_the_safety_boundaries():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "备份" in text
    assert "reload" in text  # never restart when adding to a live daemon
    assert "退出码" in text  # failures are reported verbatim
