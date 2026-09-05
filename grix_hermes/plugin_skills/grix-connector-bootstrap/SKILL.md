---
name: grix-connector-bootstrap
description: Install grix-connector on this machine and bring up its first agent, so a Hermes-only host becomes a Grix connector host. Covers the install check, the Node.js check, the global npm install, creating the platform agent via grix-admin, writing ~/.grix/config/agents.json, starting the daemon, and verifying the agent is connected.
trigger: 当用户要在这台机器上安装 Grix 连接器 / grix-connector、说"这台机器没有连接器"、"装 Grix 连接器"、"安装连接器"、或点名 grix-connector-bootstrap 时
---

# Grix Connector Bootstrap

把这台"只有 hermes"的机器变成一台跑 grix-connector 的机器：装 CLI → 建第一个
agent → 写配置 → 启动 → 验证在线。全流程用本机 shell（普通命令执行）加
`grix_invoke` 完成，**不需要**任何新协议动作或远程管理权限。

安全边界（不得越界）：

- 只安装 `grix-connector` 这一个全局 npm 包，不升级/不改动其他全局包。
- 不删除、不覆盖用户已有的 agent 配置；写 `agents.json` 前先备份。
- `api_key` 只写进 `~/.grix/config/agents.json`，不写日志、不回显到聊天、不进
  仓库或 shell 历史。
- 每步失败都**如实回报命令、退出码、stderr 尾部**，不猜原因、不重复轰炸安装。

---

## ① 检查是否已经装过

```bash
grix-connector --version
curl -s -m 3 http://127.0.0.1:19579/healthz
```

- `--version` 有输出，或 healthz 返回 `{"status":"ok",...}` → 连接器已经装好。
  直接跳到 **⑤ 验证并回报**，不要重装。
- 两个都失败（command not found + 连接被拒）→ 继续 ②。

healthz 返回体包含 `status`、`version`、`uptime`、`pid`、`agents`（每个 agent 有
`name` / `agentId` / `alive` / `wsConnected`）以及 `ws: {connected, total}`。

## ② 检查 Node.js

```bash
node --version
npm --version
```

grix-connector 要求 **Node.js >= 18**（package.json `engines`），实践中建议
Node 20 LTS 或更高。版本不满足或根本没有 Node 时**停下来问用户**，给出对应平台
的安装建议，不要自作主张改动用户的 Node 环境：

- macOS：`brew install node`，或用 nvm：`nvm install 20 && nvm use 20`
- Linux（Debian/Ubuntu）：NodeSource 20.x 源，或 nvm 同上
- Windows：`winget install OpenJS.NodeJS.LTS`，或官网 LTS 安装包

装完让用户确认后再继续，不要在等待期间往下走。

## ③ 全局安装

```bash
npm install -g grix-connector
```

失败时（国内网络常见 ETIMEDOUT / ECONNRESET），**只重试一次**，换镜像：

```bash
npm install -g grix-connector --registry=https://registry.npmmirror.com
```

再失败就停下，把命令、退出码和 stderr 最后 20 行原样回报给用户，等用户决定
（可能是权限不足需要提权、需要改 npm 全局前缀目录、或者是公司代理问题）。不要反复重试。

安装后确认：

```bash
grix-connector --version
```

## ④ 建第一个 agent 并写配置

### 4.1 确定 agent 名和 client_type

- **名字**：用用户在消息里给的名字；没给就问用户要一个，不要自己编。
- **client_type**：用用户指定的；没指定默认 `claude`。可选值：`claude`、
  `codex`、`gemini`、`qwen`、`deepseek`、`copilot`、`kiro`、`reasonix`、
  `cursor`、`codewhale`、`opencode`、`pi`、`openhuman`、`agy`、`hermes`、`acp`。
  对应的 CLI 必须已经装在本机（例如 `claude` 要能在 PATH 里找到），否则 agent
  连上了也跑不动 —— 装之前先 `which <cli>` 确认一下，缺了就在回报里点名。

### 4.2 在平台建 agent（走 grix-admin 技能）

按 `grix-admin` 技能规程调用：

```text
grix_invoke(action="agent_api_create", params={"agent_name": "<NAME>", "provider_type": 3, "is_main": true})
```

`provider_type: 3` 是 Agent API 类型 —— 只有这个类型能被连接器驱动，必须显式带上。

返回里取三个字段（可能在 `data` 里）：

| 返回字段 | 写进配置的字段 |
|---|---|
| `agent_id`（或 `id`） | `agent_id` |
| `api_key` | `api_key` —— **只出现一次，之后再也取不回来** |
| `api_endpoint`（或 `endpoint`） | `ws_url` —— 去掉 `?` 及其后面的 query string |

`api_endpoint` 为空时按区域兜底：中国大陆
`wss://grix.dhf.pub/v1/agent-api/ws`，海外 `wss://ws.grix.im/v1/agent-api/ws`。

三个字段缺任何一个都不要往下写配置，直接回报失败。

### 4.3 写 `~/.grix/config/agents.json`

配置目录固定是 `~/.grix/config`（可被 `GRIX_CONNECTOR_HOME` 覆盖，默认
`~/.grix`）。连接器会扫描该目录下**所有** `.json` 文件，取其中带 `agents` 数组
的作为 agent 配置。

条目结构（扁平，五个必填字段）：

```json
{
  "agents": [
    {
      "name": "<NAME>",
      "ws_url": "wss://grix.dhf.pub/v1/agent-api/ws",
      "agent_id": "<AGENT_ID>",
      "api_key": "<API_KEY>",
      "client_type": "claude"
    }
  ]
}
```

写入规则：

- 文件不存在 → 直接创建，`agents` 数组里只放这一个条目。
- 文件已存在 → **先备份**
  （`cp ~/.grix/config/agents.json ~/.grix/config/agents.json.bak.$(date +%s)`），
  再用脚本（`python3` / `node`）读 JSON、在 `agents` 数组里按 `agent_id` 查找：
  找到就替换该条目，没找到就追加，其余条目原样不动。**不要手写整个文件**，
  截断或重排会静默丢掉别的 agent。
- 同名 agent 不能在多个配置文件里重复定义，否则连接器会拒绝加载。

## ⑤ 启动并验证

守护进程在配置目录里没有任何有效 agent 配置时**拒绝启动**，所以必须先完成 ④
再启动。

```bash
grix-connector status     # 看守护进程是否已经在跑
```

- 没在跑 → `grix-connector start`
- 已经在跑（本次是往已有环境里加 agent）→ `grix-connector reload`
  （热加载新 agent，不打断其他 agent 的在途会话；**不要**用 `restart`）

然后轮询健康检查，最多 60 秒（每 3 秒一次，共 20 次），直到目标 agent 的
`wsConnected` 变成 `true`：

```bash
python3 - <<'EOF'
import json, time, urllib.request
NAME = "<NAME>"
for _ in range(20):
    try:
        with urllib.request.urlopen("http://127.0.0.1:19579/healthz", timeout=3) as r:
            d = json.load(r)
        hit = [a for a in d.get("agents", []) if a.get("name") == NAME]
        print(d.get("version"), hit[0].get("wsConnected") if hit else "not-found", flush=True)
        if hit and hit[0].get("wsConnected") is True:
            break
    except Exception as exc:
        print("healthz not ready:", exc, flush=True)
    time.sleep(3)
EOF
```

（`19579` 是默认健康检查端口，可被 `GRIX_HEALTH_PORT` / `--health-port` 覆盖。
另一个可选核对入口是 admin API `curl -s http://127.0.0.1:19580/api/agents`，
条目里 `"alive": true` 表示实例在跑；默认端口被覆盖时实际端口写在
`~/.grix/data/admin-port`。）

60 秒还没连上 → 不要继续等，读最新日志定位并如实回报：

```bash
ls -t ~/.grix/log/ | head -3
tail -50 ~/.grix/log/<最新日志>
tail -50 ~/.grix/service/daemon.err.log
```

常见原因：`client_type` 对应的 CLI 不在 PATH；`api_key` / `agent_id` 不匹配；
端口 19579/19580 被占用（此时 `~/.grix/daemon-status.json` 的 `reason` 会写成
`port_bind_in_use:health:19579` 之类）。

## ⑥ 回报

装好后给用户一条明确回报，包含：

1. **连接器版本** —— `grix-connector --version` 的输出。
2. **agent 名字与在线状态** —— 名字、client_type、`wsConnected` 是 true 还是
   false（false 要附上日志里的具体原因）。
3. **下一步** —— "回手机端刷新 agent 列表，就能看到这个 agent，继续后面的安装
   步骤"。

不要在回报里带上 `api_key`（哪怕是片段）。

## 失败回报格式

任何一步失败，按这个格式回报，不要脑补原因：

```text
步骤：③ npm install -g grix-connector
命令：npm install -g grix-connector --registry=https://registry.npmmirror.com
退出码：1
stderr（尾部）：
<最后 20 行原样贴出>
```
