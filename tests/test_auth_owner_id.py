"""auth_ack owner_id 解析测试（SkillSyncManager 按 owner 分桶的依据）。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402
from grix_hermes.transport import GrixTransportClient  # noqa: E402


def _client() -> GrixTransportClient:
    return GrixTransportClient(
        GrixConnectionConfig(
            endpoint="wss://example.invalid", agent_id="agent-1", api_key="secret"
        )
    )


def test_auth_ack_owner_id_parsed_into_session_and_client():
    client = _client()

    async def fake_request(cmd, payload, **kw):
        return {"payload": {"code": 0, "heartbeat_sec": 30, "owner_id": "12345"}}

    client.request = fake_request  # type: ignore[assignment]
    session = asyncio.run(client.authenticate())
    assert session.owner_id == "12345"
    # client.owner_id 属性从已认证的 session 透出。
    assert client.owner_id is None  # authenticate 本身不回写 _auth_session
    client._auth_session = session
    assert client.owner_id == "12345"


def test_auth_ack_owner_id_missing_or_numeric():
    # 旧服务端不携带 → None（adapter 据此跳过技能同步）。
    client = _client()

    async def fake_request(cmd, payload, **kw):
        return {"payload": {"code": 0, "heartbeat_sec": 30}}

    client.request = fake_request  # type: ignore[assignment]
    assert asyncio.run(client.authenticate()).owner_id is None

    # 数字 0（平台系统 owner）不能当假值丢掉。
    client2 = _client()

    async def fake_request2(cmd, payload, **kw):
        return {"payload": {"code": 0, "heartbeat_sec": 30, "owner_id": 0}}

    client2.request = fake_request2  # type: ignore[assignment]
    assert asyncio.run(client2.authenticate()).owner_id == "0"
