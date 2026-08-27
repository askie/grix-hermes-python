"""队列协议操作的 adapter 级测试（queue_snapshot_query / queue_reorder /
queue_clear / event_cancel / event_stop 摘队 / 收口释放槽位）。

走 stub 模式（同 test_event_result_timing.py），不依赖 hermes-agent host。
队列投递被替换为记录桩：只验证协议语义与队列状态，不真正跑 hermes 轮次。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "tools" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        reg = types.ModuleType("tools.registry")

        class _Registry:
            def register(self, **kw):
                pass

        reg.registry = _Registry()
        reg.tool_error = lambda msg: f"ERR:{msg}"
        reg.tool_result = lambda obj: f"OK:{obj}"
        tools_pkg.registry = reg
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg

    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

            def __eq__(self, other):
                return getattr(other, "value", None) == self.value

            def __hash__(self):
                return hash(self.value)

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

        gw_session = types.ModuleType("gateway.session")
        gw_session.build_session_key = lambda *a, **kw: "k"

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")
        gw_platforms_base.BasePlatformAdapter = object
        gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
        gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
        gw_platforms_base.ProcessingOutcome = type(
            "ProcessingOutcome", (), {"SUCCESS": object(), "CANCELLED": object()}
        )
        gw_platforms_base.SendResult = type("SendResult", (), {})

        gw_run = types.ModuleType("gateway.run")
        gw_run._gateway_runner_ref = lambda: None

        sys.modules["gateway"] = gw
        sys.modules["gateway.config"] = gw_cfg
        sys.modules["gateway.session"] = gw_session
        sys.modules["gateway.platforms"] = gw_platforms
        sys.modules["gateway.platforms.base"] = gw_platforms_base
        sys.modules["gateway.run"] = gw_run


_install_stubs()

from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.event_queue import EventQueue, EventQueueConfig, QueueItem  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class FakeTransportClient:
    """记录全部队列相关出包的 transport 假件。"""

    def __init__(self):
        self._config = SimpleNamespace(shared_owner_id=None)
        self.status = {"connected": True, "authed": True}
        self.completed = []
        self.event_states = []
        self.snapshots = []
        self.binding_cards = []
        self.packet_order = []
        self.reorder_results = []
        self.clear_results = []
        self.cancel_results = []
        self.hold_results = []
        self.edit_results = []
        self.stop_acks = []
        self.stop_results = []
        self.activities = []
        self.invokes = []

    async def complete_event(self, *, event_id, status, code=None, message=None, updated_at=None):
        self.completed.append({"event_id": event_id, "status": status, "message": message})

    async def send_event_state(self, *, event_id, session_id, state, extra=None):
        self.event_states.append(
            {"event_id": event_id, "session_id": session_id, "state": state, **(extra or {})}
        )

    async def send_queue_snapshot(self, *, session_id, running, running_items, queued):
        self.packet_order.append("queue_snapshot")
        self.snapshots.append(
            {"session_id": session_id, "running": running, "queued": queued}
        )

    async def send_update_binding_card(self, *, session_id, worker_status, cwd="", meta=None):
        self.packet_order.append("binding_card")
        self.binding_cards.append(
            {
                "session_id": session_id,
                "worker_status": worker_status,
                "cwd": cwd,
                "meta": meta,
            }
        )

    async def send_queue_reorder_result(self, *, session_id, applied_event_ids):
        self.reorder_results.append(
            {"session_id": session_id, "applied_event_ids": applied_event_ids}
        )

    async def send_queue_clear_result(self, *, session_id, success, canceled_event_ids=None, message=None):
        self.clear_results.append(
            {
                "session_id": session_id,
                "success": success,
                "canceled_event_ids": canceled_event_ids or [],
            }
        )

    async def send_event_cancel_result(self, *, event_id, accepted, reason=None, final_state=None):
        self.cancel_results.append(
            {"event_id": event_id, "accepted": accepted, "final_state": final_state, "reason": reason}
        )

    async def send_event_hold_result(self, *, session_id, event_id, ok, held=False, error=None):
        self.hold_results.append(
            {"session_id": session_id, "event_id": event_id, "ok": ok, "held": held, "error": error}
        )

    async def send_queue_edit_result(self, *, session_id, event_id, ok, error=None):
        self.edit_results.append(
            {"session_id": session_id, "event_id": event_id, "ok": ok, "error": error}
        )

    async def acknowledge_stop(self, *, event_id, accepted, stop_id=None, updated_at=None):
        self.stop_acks.append({"event_id": event_id, "accepted": accepted, "stop_id": stop_id})

    async def complete_stop(self, *, event_id, status, stop_id=None, code=None, message=None, updated_at=None):
        self.stop_results.append({"event_id": event_id, "status": status, "stop_id": stop_id})

    async def set_session_activity(self, *, session_id, kind, active, ttl_ms=None, **kwargs):
        self.activities.append(
            {"session_id": session_id, "kind": kind, "active": active, "ttl_ms": ttl_ms}
        )

    async def agent_invoke(self, *, action, params=None, timeout_ms=None):
        self.invokes.append({"action": action, "params": params or {}})
        return {"ok": True}


def _make_adapter(client=None):
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    from collections import defaultdict

    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._shared_clients = {}
    inst._shutting_down = False
    inst._disconnect_requested = False
    inst._client = client or FakeTransportClient()
    inst._pending_messages = adapter_mod._PendingMessagesDict(inst._on_pending_consumed)
    inst._active_sessions = {}
    inst._inflight_dispatch_event_ids = {}
    inst._event_queue = EventQueue(
        EventQueueConfig(max_queued=5, queue_timeout_ms=0),
        on_deliver=inst._on_queue_deliver,
        on_state_change=inst._on_queue_state_change,
    )
    # 投递桩：记录被投递的事件，不真正跑 hermes 轮次。
    inst._delivered = []

    async def _fake_dispatch(message, source, session_key):
        inst._delivered.append(message.event_id)

    inst._dispatch_grix_event = _fake_dispatch
    inst._bg_hold_sweep_task = None
    inst._toolbar_pending_model_sessions = set()
    inst._toolbar_models_loaded = False
    inst._toolbar_models_task = None
    inst._ensure_toolbar_models_refresh = lambda: None
    inst._BG_HOLD_MAX_AGE_S = adapter_mod.GrixAdapter._BG_HOLD_MAX_AGE_S
    inst._BG_HOLD_SWEEP_INTERVAL_S = adapter_mod.GrixAdapter._BG_HOLD_SWEEP_INTERVAL_S
    inst._BG_HOLD_COMPOSING_TTL_MS = adapter_mod.GrixAdapter._BG_HOLD_COMPOSING_TTL_MS
    return inst


def _install_process_registry(monkeypatch, sessions):
    proc_mod = types.ModuleType("tools.process_registry")

    class _ProcessRegistry:
        def list_sessions(self, session_key=None):
            return list(sessions)

    proc_mod.process_registry = _ProcessRegistry()
    monkeypatch.setitem(sys.modules, "tools.process_registry", proc_mod)
    if "tools" in sys.modules:
        monkeypatch.setattr(sys.modules["tools"], "process_registry", proc_mod, raising=False)


def _item(inst, event_id, session_id="s1", group_key="g1", owner_key="", text=""):
    message = SimpleNamespace(event_id=event_id, session_id=session_id, text=text or event_id)
    source = SimpleNamespace(chat_id=session_id, thread_id=None)
    return QueueItem(
        event_id=event_id,
        session_id=session_id,
        group_key=group_key,
        owner_key=owner_key,
        preview=text or event_id,
        payload=(message, source, group_key),
    )


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


def _run_with_ctx(inst, client, coro):
    async def _wrapped():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            result = await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
        await _settle()
        return result

    return asyncio.run(_wrapped())


# ── 快照查询 ────────────────────────────────────────────────────────────


def test_snapshot_query_returns_snapshot_even_when_empty():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )
    assert client.snapshots[-1] == {"session_id": "s1", "running": [], "queued": []}


def test_snapshot_reports_single_immutable_configured_model():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._toolbar_model_id = "anthropic/claude-sonnet-4"

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )

    assert client.binding_cards[-1] == {
        "session_id": "s1",
        "worker_status": "ready",
        "cwd": "",
        "meta": {
            "model_id": "anthropic/claude-sonnet-4",
            "available_models": [
                {
                    "id": "anthropic/claude-sonnet-4",
                    "displayName": "anthropic/claude-sonnet-4",
                }
            ],
        },
    }
    assert client.packet_order[-2:] == ["binding_card", "queue_snapshot"]


def test_snapshot_reports_switchable_toolbar_models():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._toolbar_model_id = "deepseek-v4-flash"
    inst._toolbar_model_provider = "opencode-go"
    inst._toolbar_available_models = [
        {
            "id": "deepseek-v4-pro",
            "displayName": "deepseek-v4-pro",
            "provider": "opencode-go",
            "providerLabel": "OpenCode Go",
            "selectionCommand": "/model deepseek-v4-pro --provider opencode-go",
        },
        {
            "id": "deepseek-v4-flash",
            "displayName": "deepseek-v4-flash",
            "provider": "opencode-go",
            "providerLabel": "OpenCode Go",
            "selectionCommand": "/model deepseek-v4-flash --provider opencode-go",
        },
    ]

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )

    meta = client.binding_cards[-1]["meta"]
    assert meta["model_id"] == "deepseek-v4-flash"
    assert meta["model_provider"] == "opencode-go"
    assert meta["provider_id"] == "opencode-go"
    assert meta["available_models"] == inst._toolbar_available_models
    assert meta["available_providers"] == [{"id": "opencode-go", "displayName": "OpenCode Go"}]


def test_snapshot_filters_models_to_current_provider():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._toolbar_model_id = "kimi-k2"
    inst._toolbar_model_provider = "kimi"
    inst._toolbar_available_models = [
        {"id": "deepseek-v4-pro", "displayName": "deepseek-v4-pro", "provider": "deepseek", "providerLabel": "DeepSeek"},
        {"id": "kimi-k2", "displayName": "kimi-k2", "provider": "kimi", "providerLabel": "Kimi"},
    ]

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )

    meta = client.binding_cards[-1]["meta"]
    assert [m["id"] for m in meta["available_models"]] == ["kimi-k2"]
    assert [p["id"] for p in meta["available_providers"]] == ["deepseek", "kimi"]


def test_inventory_refresh_repushes_empty_new_session_and_releases_tracking(monkeypatch):
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._toolbar_model_id = "deepseek-v4-flash"
    inst._toolbar_available_models = [
        {"id": "deepseek-v4-flash", "displayName": "deepseek-v4-flash"}
    ]

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )
    assert ("s1", "") in inst._toolbar_pending_model_sessions

    refreshed_models = [
        {"id": "deepseek-v4-pro", "displayName": "deepseek-v4-pro"},
        {"id": "deepseek-v4-flash", "displayName": "deepseek-v4-flash"},
    ]
    monkeypatch.setattr(adapter_mod, "resolve_toolbar_available_models", lambda: refreshed_models)
    monkeypatch.setattr(adapter_mod, "resolve_configured_model", lambda: "deepseek-v4-flash")
    monkeypatch.setattr(adapter_mod, "resolve_configured_provider", lambda: "opencode-go")
    client.binding_cards.clear()
    _run_with_ctx(inst, client, inst._refresh_toolbar_models_once())

    assert client.binding_cards[-1]["session_id"] == "s1"
    assert len(client.binding_cards[-1]["meta"]["available_models"]) == 2
    assert inst._toolbar_pending_model_sessions == set()
    assert inst._toolbar_models_loaded is True


# 回归锚：provider-quota 平移后，缓存的厂商配额必须随工具栏绑定卡下发
# （provider_quota + rate_limits），否则服务端工具栏拿不到 5h/周限额。
def test_snapshot_binding_card_enriches_provider_quota():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._toolbar_model_id = "deepseek-v4-flash"
    inst._provider_quota = {
        "provider": "kimi",
        "providerLabel": "Kimi",
        "planName": None,
        "tiers": [
            {"name": "five_hour", "label": "5h", "usedPercent": 12.5, "resetsAt": None},
            {"name": "weekly_limit", "label": "W", "usedPercent": 30.0, "resetsAt": None},
        ],
        "balance": None,
        "success": True,
        "error": None,
    }
    inst._provider_quota_sampled_at_ms = 1700000000000

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )

    meta = client.binding_cards[-1]["meta"]
    assert meta["model_id"] == "deepseek-v4-flash"
    assert meta["provider_quota"]["provider"] == "kimi"
    assert meta["rate_limits"]["fiveHour"]["usedPercentage"] == 12.5
    assert meta["rate_limits"]["sevenDay"]["usedPercentage"] == 30.0
    assert meta["rate_limits"]["sampledAt"] == 1700000000000


def test_resolve_configured_model_supports_current_and_legacy_shapes(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  default: openrouter/anthropic/claude-opus-4\n")
    assert adapter_mod.resolve_configured_model(str(tmp_path)) == "openrouter/anthropic/claude-opus-4"

    config_path.write_text("model: nous/hermes-4-70b\n")
    assert adapter_mod.resolve_configured_model(str(tmp_path)) == "nous/hermes-4-70b"

    config_path.write_text("model: [unterminated\n")
    assert adapter_mod.resolve_configured_model(str(tmp_path)) == ""


def test_resolve_toolbar_available_models_uses_hermes_inventory(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  default: deepseek-v4-flash\n  provider: opencode-go\n"
        "providers:\n  deepseek-api:\n    default_model: deepseek-v4-flash\n"
        "  nvidia:\n    name: NVIDIA\n    default_model: openai/gpt-oss-120b\n",
        encoding="utf-8",
    )
    hermes_pkg = types.ModuleType("hermes_cli")
    inventory = types.ModuleType("hermes_cli.inventory")
    inventory.load_picker_context = lambda: object()

    def _build_models_payload(*args, **kwargs):
        return {
            "provider": "opencode-go",
            "model": "deepseek-v4-flash",
            "providers": [
                {"slug": "zai", "name": "Z.AI", "models": ["glm-5"]},
                {
                    "slug": "deepseek-api",
                    "name": "DeepSeek API",
                    "models": ["deepseek-v4-flash"],
                },
                {
                    "slug": "opencode-go",
                    "name": "OpenCode Go",
                    "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
                },
            ],
        }

    inventory.build_models_payload = _build_models_payload
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.inventory", inventory)

    models = adapter_mod.resolve_toolbar_available_models(str(tmp_path))

    assert models == [
        {
            "id": "openai/gpt-oss-120b",
            "displayName": "openai/gpt-oss-120b",
            "provider": "nvidia",
            "selectionCommand": "/model openai/gpt-oss-120b --provider nvidia",
            "providerLabel": "NVIDIA",
        },
        {
            "id": "deepseek-v4-pro",
            "displayName": "deepseek-v4-pro",
            "provider": "opencode-go",
            "selectionCommand": "/model deepseek-v4-pro --provider opencode-go",
            "providerLabel": "OpenCode Go",
        },
        {
            "id": "deepseek-v4-flash",
            "displayName": "deepseek-v4-flash",
            "provider": "opencode-go",
            "selectionCommand": "/model deepseek-v4-flash --provider opencode-go",
            "providerLabel": "OpenCode Go",
        },
        {
            "id": "deepseek-v4-flash",
            "displayName": "deepseek-v4-flash",
            "provider": "deepseek-api",
            "selectionCommand": "/model deepseek-v4-flash --provider deepseek-api",
            "providerLabel": "DeepSeek API",
        },
    ]


def test_resolve_toolbar_models_maps_configured_custom_provider(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: z-model\n  provider: custom:local\n"
        "custom_providers:\n  local:\n    name: Local\n"
        "    models:\n      - z-model\n      - a-model\n",
        encoding="utf-8",
    )
    hermes_pkg = types.ModuleType("hermes_cli")
    inventory = types.ModuleType("hermes_cli.inventory")
    inventory.load_picker_context = lambda: object()
    inventory.build_models_payload = lambda *args, **kwargs: {
        "provider": "custom:local",
        "providers": [
            {"slug": "openai-codex", "models": ["gpt-5.4"]},
            {
                "slug": "custom:local",
                "name": "Local",
                "models": ["z-model", "m-model"],
            },
        ],
    }
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.inventory", inventory)

    models = adapter_mod.resolve_toolbar_available_models(str(tmp_path))

    assert [entry["id"] for entry in models] == ["z-model", "m-model", "a-model"]
    assert {entry["provider"] for entry in models} == {"custom:local"}


def test_resolve_toolbar_models_uses_configured_defaults_when_inventory_fails(
    tmp_path, monkeypatch
):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: deepseek-v4-flash\n  provider: opencode-go\n"
        "providers:\n  nvidia:\n    name: NVIDIA\n"
        "    default_model: openai/gpt-oss-120b\n",
        encoding="utf-8",
    )
    hermes_pkg = types.ModuleType("hermes_cli")
    inventory = types.ModuleType("hermes_cli.inventory")
    inventory.load_picker_context = lambda: object()

    def _failed_inventory(*args, **kwargs):
        raise RuntimeError("inventory unavailable")

    inventory.build_models_payload = _failed_inventory
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.inventory", inventory)

    models = adapter_mod.resolve_toolbar_available_models(str(tmp_path))

    assert [entry["id"] for entry in models] == [
        "openai/gpt-oss-120b",
        "deepseek-v4-flash",
    ]
    assert [entry["provider"] for entry in models] == ["nvidia", "opencode-go"]


def test_snapshot_query_reports_running_and_queued():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1", text="first job"))
        inst._event_queue.submit(_item(inst, "e2", text="second job"))
        await _settle()
        await inst._handle_queue_snapshot_query_packet({"session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    snap = client.snapshots[-1]
    assert snap["running"] == ["e1"]
    assert [q["event_id"] for q in snap["queued"]] == ["e2"]
    assert snap["queued"][0]["position"] == 1
    assert snap["queued"][0]["actions"] == [{"type": "cancel"}]
    # 排队事件的 event_state 已上报（带位置与可取消操作）
    queued_states = [s for s in client.event_states if s["state"] == "queued"]
    assert queued_states and queued_states[-1]["event_id"] == "e2"
    assert queued_states[-1]["queue_position"] == 1
    assert queued_states[-1]["actions"] == [{"type": "cancel"}]


def test_snapshot_query_reports_virtual_running_for_active_framework_work():
    """显式队列为空时，Hermes 正在处理的轮次仍应让工具栏显示 1 个任务。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._state_for("").toolbar_active_work["sk:s1"] = {
        "session_id": "s1",
        "title": "background job",
    }

    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )

    assert client.snapshots[-1]["running"] == ["selfdrive_s1"]


def test_real_running_item_takes_priority_over_virtual_framework_work():
    """已有真实 running 事件时不重复合成虚拟任务，避免计数虚高。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._state_for("").toolbar_active_work["sk:s1"] = {
        "session_id": "s1",
        "title": "background job",
    }

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1", text="first job"))
        await _settle()
        await inst._handle_queue_snapshot_query_packet({"session_id": "s1"})

    _run_with_ctx(inst, client, _flow())

    assert client.snapshots[-1]["running"] == ["e1"]


def test_reconnect_replays_virtual_running_snapshot():
    """重连补推不能只遍历显式队列，否则自驱任务会再次显示为 0。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._state_for("").toolbar_active_work["sk:s1"] = {
        "session_id": "s1",
        "title": "background job",
    }

    _run_with_ctx(inst, client, inst._push_all_queue_snapshots())

    assert client.snapshots[-1]["running"] == ["selfdrive_s1"]


def test_processing_complete_keeps_virtual_running_while_bg_process_alive(monkeypatch):
    """LLM 轮次收口后若 Hermes 后台进程仍在，工具栏虚拟 running 必须保留。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._session_has_bg_hold = lambda session_key: session_key == "sk:s1"
    inst._bg_hold_label = lambda session_key: "release.sh server"
    inst._ensure_bg_hold_sweep = lambda: None

    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="s1", thread_id=None),
        message_id="m1",
        text="publish",
        raw_message={"_grix_kind": "message", "event_id": "ev-1"},
    )

    async def _flow():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS)

    _run_with_ctx(inst, client, _flow())

    work = inst._state_for("").toolbar_active_work["sk:s1"]
    assert work["bg_hold"] is True
    assert work["title"] == "release.sh server"
    assert client.completed[-1]["status"] == "responded"
    assert any(a["active"] is True for a in client.activities)
    # 收口路径上不得出现空 running 快照（否则前端会先清队列）
    assert all(snap["running"] for snap in client.snapshots), client.snapshots
    assert client.snapshots[-1]["running"] == ["selfdrive_s1"]
    assert not any(i["action"] == "chat_state_update" for i in client.invokes)


def test_bg_hold_ignores_silent_long_lived_daemon(monkeypatch):
    """无完成通知/watch 的常驻后台进程不应让本轮对话一直 running。"""
    inst = _make_adapter()
    _install_process_registry(monkeypatch, [
        {
            "session_id": "proc-daemon",
            "command": "cd /Volumes/disk1/go/src/grix-connector && node dist/grix.js",
            "status": "running",
            "uptime_seconds": 60,
        }
    ])

    assert inst._session_has_bg_hold("sk:s1") is False
    assert inst._bg_hold_label("sk:s1") == ""


def test_bg_hold_keeps_notified_background_process(monkeypatch):
    """会产生完成通知的后台任务仍保留虚拟 running。"""
    inst = _make_adapter()
    _install_process_registry(monkeypatch, [
        {
            "session_id": "proc-build",
            "command": "npm test -- --watch=false",
            "status": "running",
            "uptime_seconds": 60,
            "notify_on_complete": True,
        }
    ])

    assert inst._session_has_bg_hold("sk:s1") is True
    assert inst._bg_hold_label("sk:s1") == "npm test -- --watch=false"


def test_processing_complete_clears_virtual_running_for_silent_daemon(monkeypatch):
    """事故回归：常驻 daemon 仍在 process_registry 里，也不能阻塞本轮收口。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_process_registry(monkeypatch, [
        {
            "session_id": "proc-daemon",
            "command": "cd /Volumes/disk1/go/src/grix-connector && node dist/grix.js",
            "status": "running",
            "uptime_seconds": 60,
        }
    ])

    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="s1", thread_id=None),
        message_id="m1",
        text="restart connector",
        raw_message={"_grix_kind": "message", "event_id": "ev-1"},
    )

    async def _flow():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS)

    _run_with_ctx(inst, client, _flow())

    assert "sk:s1" not in inst._state_for("").toolbar_active_work
    assert client.completed[-1]["status"] == "responded"
    assert client.snapshots[-1]["running"] == []


def test_processing_complete_clears_virtual_running_without_bg_process(monkeypatch):
    """无后台进程时 complete 仍清掉虚拟 running（旧行为）。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._session_has_bg_hold = lambda session_key: False

    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="s1", thread_id=None),
        message_id="m1",
        text="hi",
        raw_message={"_grix_kind": "message", "event_id": "ev-1"},
    )

    async def _flow():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS)

    _run_with_ctx(inst, client, _flow())

    assert "sk:s1" not in inst._state_for("").toolbar_active_work
    assert client.snapshots[-1]["running"] == []


def test_processing_complete_cancel_does_not_bg_hold(monkeypatch):
    """用户取消本轮时不保活后台虚拟项。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._session_has_bg_hold = lambda session_key: True
    inst._bg_hold_label = lambda session_key: "should-not-hold"

    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="s1", thread_id=None),
        message_id="m1",
        text="stop",
        raw_message={"_grix_kind": "message", "event_id": "ev-1"},
    )

    async def _flow():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.CANCELLED)

    _run_with_ctx(inst, client, _flow())

    assert "sk:s1" not in inst._state_for("").toolbar_active_work
    assert client.snapshots[-1]["running"] == []
    assert client.completed[-1]["status"] == "canceled"


def test_bg_hold_sweep_clears_when_process_exits(monkeypatch):
    """巡检发现后台进程已退出后，清虚拟 running。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._state_for("").toolbar_active_work["sk:s1"] = {
        "session_id": "s1",
        "title": "release.sh",
        "bg_hold": True,
    }
    inst._session_has_bg_hold = lambda session_key: False

    _run_with_ctx(inst, client, inst._sweep_bg_holds_once())

    assert "sk:s1" not in inst._state_for("").toolbar_active_work
    assert client.snapshots[-1]["running"] == []
    assert any(a["active"] is False for a in client.activities)
    assert not any(i["action"] == "chat_state_update" for i in client.invokes)


def test_bg_hold_survives_queue_complete_mid_snapshot(monkeypatch):
    """queue.complete 中途推快照时，bg_hold 已就位，不得发出空 running。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", lambda *a, **kw: "sk:s1")
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._session_has_bg_hold = lambda session_key: True
    inst._bg_hold_label = lambda session_key: "release.sh"
    inst._ensure_bg_hold_sweep = lambda: None

    # 真实 running 项：complete 时会 push 一次快照
    item = _item(inst, "ev-1", session_id="s1", text="publish")
    inst._event_queue.submit(item)

    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="s1", thread_id=None),
        message_id="m1",
        text="publish",
        raw_message={"_grix_kind": "message", "event_id": "ev-1"},
    )

    async def _flow():
        await _settle()
        await inst.on_processing_start(event)
        client.snapshots.clear()
        await inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS)

    _run_with_ctx(inst, client, _flow())

    assert client.snapshots, "expected at least one snapshot during complete"
    assert all(snap["running"] for snap in client.snapshots), client.snapshots
    assert inst._state_for("").toolbar_active_work["sk:s1"]["bg_hold"] is True


# ── 重排 ────────────────────────────────────────────────────────────────


def test_reorder_applies_and_reports_result():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        for eid in ("e1", "e2", "e3", "e4"):
            inst._event_queue.submit(_item(inst, eid))
        await inst._handle_queue_reorder_packet(
            {"session_id": "s1", "ordered_event_ids": ["e4", "e2", "gone"]}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.reorder_results == [
        {"session_id": "s1", "applied_event_ids": ["e4", "e2", "e3"]}
    ]
    snap = client.snapshots[-1]
    assert [q["event_id"] for q in snap["queued"]] == ["e4", "e2", "e3"]


# ── 清空 ────────────────────────────────────────────────────────────────


def test_queue_clear_cancels_queued_only():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        inst._event_queue.submit(_item(inst, "e3"))
        await inst._handle_queue_clear_packet({"session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert client.clear_results == [
        {"session_id": "s1", "success": True, "canceled_event_ids": ["e2", "e3"]}
    ]
    # 排队事件以 canceled 收口（event_result），运行中的 e1 不动
    canceled = {c["event_id"] for c in client.completed if c["status"] == "canceled"}
    assert canceled == {"e2", "e3"}
    assert inst._event_queue.is_running("e1")


# ── 单事件取消 ──────────────────────────────────────────────────────────


def test_event_cancel_removes_queued_item():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_event_cancel_packet({"event_id": "e2", "session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert client.cancel_results == [
        {"event_id": "e2", "accepted": True, "final_state": "canceled", "reason": None}
    ]
    assert [c for c in client.completed if c["event_id"] == "e2"] == [
        {"event_id": "e2", "status": "canceled", "message": "canceled by user"}
    ]
    assert not inst._event_queue.is_queued("e2")
    assert inst._event_queue.is_running("e1")


def test_event_cancel_running_stops_only_that_group():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    stopped = []

    async def _fake_force_stop(source, session_key, **kw):
        stopped.append(session_key)
        return True

    inst._force_stop_session = _fake_force_stop

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_event_cancel_packet({"event_id": "e1", "session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert stopped == ["g1"]
    assert client.cancel_results[-1]["accepted"] is True
    assert client.cancel_results[-1]["final_state"] == "canceled"
    # e1 已被兜底收口为 canceled，槽位释放后 e2 被续投
    assert {"event_id": "e1", "status": "canceled", "message": "event canceled by user"} in client.completed
    assert inst._delivered[-1] == "e2"


def test_event_cancel_unknown_event_rejected():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _run_with_ctx(
        inst, client, inst._handle_event_cancel_packet({"event_id": "nope", "session_id": "s1"})
    )
    assert client.cancel_results == [
        {
            "event_id": "nope",
            "accepted": False,
            "final_state": None,
            "reason": "event not found or not cancelable",
        }
    ]


# ── event_stop 精确摘除排队事件 ─────────────────────────────────────────


def test_event_stop_removes_queued_event_silently():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_stop_packet(
            {"event_id": "e2", "session_id": "s1", "stop_id": "stop-1"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.stop_acks == [{"event_id": "e2", "accepted": True, "stop_id": "stop-1"}]
    assert client.stop_results == [{"event_id": "e2", "status": "stopped", "stop_id": "stop-1"}]
    # 静默摘除：不发 canceled 终态（对齐 connector），运行中的 e1 不受影响
    assert all(c["event_id"] != "e2" for c in client.completed)
    assert inst._event_queue.is_running("e1")
    assert not inst._event_queue.is_queued("e2")


# ── 队列前置拦截：clarify 文本回答 / 斜杠命令绕行 ───────────────────────


def _prepare_packet_adapter(monkeypatch, client):
    """装一个能把 event_msg 包跑到队列门控的 adapter（其余依赖打桩）。"""
    monkeypatch.setattr(
        adapter_mod, "build_session_key", lambda source, **kw: f"sk:{source.chat_id}"
    )
    inst = _make_adapter(client)
    inst.build_source = lambda **kw: SimpleNamespace(
        chat_id=kw.get("chat_id"),
        chat_type=kw.get("chat_type", "dm"),
        user_id=kw.get("user_id"),
        thread_id=kw.get("thread_id"),
        chat_name=kw.get("chat_name"),
        chat_topic=kw.get("chat_topic"),
    )
    inst._schedule_session_route_bind = lambda **kw: None

    async def _ack(**kw):
        return None

    client.acknowledge_event = _ack
    return inst


def _text_packet(event_id, content, session_id="s1"):
    return {
        "event_id": event_id,
        "session_id": session_id,
        "msg_id": f"m-{event_id}",
        "sender_id": "u1",
        "event_type": "user_chat",
        "content": content,
    }


def _install_fake_clarify(resolved, calls):
    module = types.ModuleType("tools.clarify_gateway")

    def resolve_text_response_for_session(session_key, response):
        calls.append((session_key, response))
        return resolved

    module.resolve_text_response_for_session = resolve_text_response_for_session
    sys.modules["tools.clarify_gateway"] = module
    return module


def test_clarify_text_answer_bypasses_queue(monkeypatch):
    """clarify 阻塞占槽时，打字回答必须直接解锁 clarify 而不是排队等超时。"""
    client = FakeTransportClient()
    inst = _prepare_packet_adapter(monkeypatch, client)
    calls = []
    _install_fake_clarify(True, calls)
    try:
        # clarify 所在轮次占着 sk:s1 槽位
        inst._event_queue.submit(_item(inst, "e-run", group_key="sk:s1"))
        _run_with_ctx(
            inst, client, inst._handle_message_packet(_text_packet("e-ans", "选方案A"))
        )
    finally:
        sys.modules.pop("tools.clarify_gateway", None)

    assert calls == [("sk:s1", "选方案A")]
    # 回答事件即刻收口，不入队
    assert {"event_id": "e-ans", "status": "responded", "message": None} in client.completed
    assert inst._event_queue.queued_count == 0
    assert inst._delivered == []


def test_text_without_pending_clarify_goes_through_queue(monkeypatch):
    """没有待答 clarify 时，普通文本照常走队列（空闲即投递）。"""
    client = FakeTransportClient()
    inst = _prepare_packet_adapter(monkeypatch, client)
    calls = []
    _install_fake_clarify(False, calls)
    try:
        _run_with_ctx(
            inst, client, inst._handle_message_packet(_text_packet("e-1", "正常消息"))
        )
    finally:
        sys.modules.pop("tools.clarify_gateway", None)

    assert calls and calls[0][1] == "正常消息"
    assert inst._delivered == ["e-1"]
    assert inst._event_queue.is_running("e-1")


def test_slash_command_bypasses_queue_when_busy(monkeypatch):
    """斜杠命令绕过队列直接投递：网关支持忙时处理命令，不能被长任务卡住。"""
    client = FakeTransportClient()
    inst = _prepare_packet_adapter(monkeypatch, client)
    inst._event_queue.submit(_item(inst, "e-run", group_key="sk:s1"))
    _run_with_ctx(
        inst, client, inst._handle_message_packet(_text_packet("e-cmd", "/status"))
    )
    # 命令即时投递，不占队列
    assert inst._delivered[-1] == "e-cmd"
    assert inst._event_queue.queued_count == 0
    assert not inst._event_queue.is_running("e-cmd")


# ── 审查修复回归：撤回摘队 / 投递竞态 / 编辑排队消息 / 取消回退 ─────────


def test_revoke_removes_queued_event(monkeypatch):
    """排队中消息被撤回：必须从队列摘除，不能轮到它时再投给 agent。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._event_queue.submit(_item(inst, "e1"))
    inst._event_queue.submit(_item(inst, "e2"))
    state = inst._owner_states[""]
    state.reply_event_ids[("s1", "m2")] = "e2"

    async def _ack(**kw):
        return None

    client.acknowledge_event = _ack
    _run_with_ctx(
        inst,
        client,
        inst._handle_revoke_packet(
            {"event_id": "rv-1", "session_id": "s1", "msg_id": "m2"}
        ),
    )
    assert not inst._event_queue.is_queued("e2")
    # 槽位上运行中的 e1 不受影响；e2 不会再被投递
    assert inst._event_queue.is_running("e1")


def test_canceled_before_delivery_task_runs_is_not_dispatched():
    """竞态守卫：投递任务调度后、运行前事件被收口 → 放弃投递。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        # 投递任务已 create_task 但尚未运行，事件先被收口（模拟取消抢先）
        inst._event_queue.complete("e1")
        await _settle()

    _run_with_ctx(inst, client, _flow())
    assert inst._delivered == []


def test_edit_updates_queued_message(monkeypatch):
    """排队中消息被编辑：payload 文本就地替换，执行时用编辑后内容。"""
    from grix_hermes.protocol import normalize_inbound_message

    client = FakeTransportClient()
    inst = _prepare_packet_adapter(monkeypatch, client)
    message = normalize_inbound_message(_text_packet("e2", "旧内容"))
    source = SimpleNamespace(chat_id="s1", thread_id=None)
    inst._event_queue.submit(_item(inst, "e1"))
    inst._event_queue.submit(
        QueueItem(
            event_id="e2",
            session_id="s1",
            group_key="g1",
            owner_key="",
            preview="旧内容",
            payload=(message, source, "g1"),
        )
    )
    _run_with_ctx(
        inst,
        client,
        inst._handle_edit_packet(
            {"session_id": "s1", "msg_id": "m-e2", "content": "新内容"}
        ),
    )
    queued = inst._event_queue.find("e2")
    assert queued is not None
    assert queued.payload[0].text == "新内容"
    assert queued.preview == "新内容"


def test_event_cancel_falls_back_to_session_stop_for_untracked_event():
    """队列不认识的事件（斜杠命令绕行直投）取消时回退旧语义：停会话轮次。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._active_sessions["sk:s1"] = object()
    stopped = []

    async def _fake_force_stop(source, session_key, **kw):
        stopped.append(session_key)
        return True

    inst._force_stop_session = _fake_force_stop
    inst.build_source = lambda **kw: SimpleNamespace(
        chat_id=kw.get("chat_id"), chat_type=kw.get("chat_type", "dm"),
        user_id=None, thread_id=None,
    )
    _run_with_ctx(
        inst,
        client,
        inst._handle_event_cancel_packet({"event_id": "cmd-ev", "session_id": "s1"}),
    )
    assert stopped == ["sk:s1"]
    assert client.cancel_results[-1]["accepted"] is True
    assert {"event_id": "cmd-ev", "status": "canceled", "message": "event canceled by user"} in client.completed


# ── 收口即释放槽位并续投 ─────────────────────────────────────────────────


def test_complete_event_releases_slot_and_drains_next():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await _settle()
        assert inst._delivered == ["e1"]
        await inst._complete_event_if_needed("e1", status="responded")

    _run_with_ctx(inst, client, _flow())
    assert inst._delivered == ["e1", "e2"]
    assert inst._event_queue.is_running("e2")
    assert {"event_id": "e1", "status": "responded", "message": None} in client.completed
    # e2 进入 running 的状态已上报
    running_states = [s for s in client.event_states if s["state"] == "running"]
    assert any(s["event_id"] == "e2" for s in running_states)


# ── event_hold 暂停/恢复 ────────────────────────────────────────────────


def test_event_hold_holds_then_releases_queued_item():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await _settle()
        await inst._handle_event_hold_packet(
            {"session_id": "s1", "event_id": "e2", "hold": True, "reason": "manual"}
        )
        item = inst._event_queue.find("e2")
        assert item.held is True and item.held_reason == "manual"
        # 槽位释放后 held 项挡住本组出队
        await inst._complete_event_if_needed("e1", status="responded")
        await _settle()
        assert inst._delivered == ["e1"]
        await inst._handle_event_hold_packet(
            {"session_id": "s1", "event_id": "e2", "hold": False}
        )
        await _settle()

    _run_with_ctx(inst, client, _flow())
    assert client.hold_results[0] == {
        "session_id": "s1", "event_id": "e2", "ok": True, "held": True, "error": None,
    }
    assert client.hold_results[1] == {
        "session_id": "s1", "event_id": "e2", "ok": True, "held": False, "error": None,
    }
    # 解除后 e2 续投
    assert inst._delivered == ["e1", "e2"]
    # hold 期间推过带 held 标记的快照
    assert any(
        any(q.get("held") and q.get("held_reason") == "manual" for q in snap["queued"])
        for snap in client.snapshots
    )


def test_event_hold_running_item_not_found():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        await inst._handle_event_hold_packet(
            {"session_id": "s1", "event_id": "e1", "hold": True, "reason": "manual"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.hold_results == [
        {"session_id": "s1", "event_id": "e1", "ok": False, "held": False, "error": "not_found"}
    ]
    assert inst._event_queue.is_running("e1")


def test_event_hold_missing_event_id_bad_request():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _run_with_ctx(
        inst, client, inst._handle_event_hold_packet({"session_id": "s1", "hold": True})
    )
    assert client.hold_results == [
        {"session_id": "s1", "event_id": "", "ok": False, "held": False, "error": "bad_request"}
    ]


def test_event_hold_owner_isolation():
    """共享场景：其他 owner 名下的排队事件对当前 owner 不可见（not_found）。"""
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2", owner_key="other"))
        await inst._handle_event_hold_packet(
            {"session_id": "s1", "event_id": "e2", "hold": True, "reason": "manual"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.hold_results[-1]["ok"] is False
    assert client.hold_results[-1]["error"] == "not_found"
    assert inst._event_queue.find("e2").held is False


# ── queue_edit 改写排队任务文本 ─────────────────────────────────────────


def test_queue_edit_rewrites_text_payload_and_releases(monkeypatch):
    from grix_hermes.protocol import normalize_inbound_message

    client = FakeTransportClient()
    inst = _prepare_packet_adapter(monkeypatch, client)
    message = normalize_inbound_message(_text_packet("e2", "旧任务内容"))
    source = SimpleNamespace(chat_id="s1", thread_id=None)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(
            QueueItem(
                event_id="e2",
                session_id="s1",
                group_key="g1",
                owner_key="",
                preview="旧任务内容",
                payload=(message, source, "g1"),
                content="旧任务内容",
            )
        )
        # 编辑流程先 hold（前端点编辑自动发），再改文
        await inst._handle_event_hold_packet(
            {"session_id": "s1", "event_id": "e2", "hold": True, "reason": "editing"}
        )
        await inst._handle_queue_edit_packet(
            {"session_id": "s1", "event_id": "e2", "content": "新任务全文"}
        )
        await _settle()

    _run_with_ctx(inst, client, _flow())
    assert client.edit_results == [
        {"session_id": "s1", "event_id": "e2", "ok": True, "error": None}
    ]
    queued = inst._event_queue.find("e2")
    assert queued.content == "新任务全文"
    assert queued.preview == "新任务全文"
    # payload 里冻结的入站消息同步改写：执行时用编辑后文本
    assert queued.payload[0].text == "新任务全文"
    # 编辑自动解除 hold
    assert queued.held is False and queued.held_reason == ""
    # 成功后推过带新全文的快照
    assert any(
        any(q.get("content") == "新任务全文" for q in snap["queued"])
        for snap in client.snapshots
    )


def test_queue_edit_empty_content_rejected():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2", text="原文"))
        await inst._handle_queue_edit_packet(
            {"session_id": "s1", "event_id": "e2", "content": "   "}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.edit_results == [
        {"session_id": "s1", "event_id": "e2", "ok": False, "error": "empty_content"}
    ]
    assert inst._event_queue.find("e2").preview == "原文"


def test_queue_edit_running_item_not_found():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        await inst._handle_queue_edit_packet(
            {"session_id": "s1", "event_id": "e1", "content": "新文"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.edit_results == [
        {"session_id": "s1", "event_id": "e1", "ok": False, "error": "not_found"}
    ]


def test_queue_edit_owner_isolation():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2", owner_key="other", text="别人的任务"))
        await inst._handle_queue_edit_packet(
            {"session_id": "s1", "event_id": "e2", "content": "越权改写"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.edit_results[-1]["error"] == "not_found"
    assert inst._event_queue.find("e2").preview == "别人的任务"
