# grix-hermes

Grix/aibot protocol platform adapter plugin for Hermes Agent.

## Installation

```bash
pip install grix-hermes
```

## 获取连接参数

访问 [https://grix.dhf.pub/](https://grix.dhf.pub/)，在 **AI** 标签页中创建一个 **API Agent** 类型的 Agent，即可获得以下三个参数：

- `GRIX_ENDPOINT` — WebSocket 连接地址
- `GRIX_AGENT_ID` — Agent ID
- `GRIX_API_KEY` — API Key

## Setup

将获得的参数配置为环境变量（或通过 Hermes 配置文件设置）：

```bash
export GRIX_ENDPOINT=wss://your-grix-endpoint
export GRIX_AGENT_ID=your-agent-id
export GRIX_API_KEY=your-api-key
```

Then enable the plugin in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - grix-platform
```

Or install as a user plugin:

```bash
cp -r grix_hermes ~/.hermes/plugins/grix-platform/
```

## Provided Tools

| Tool | Description |
|------|-------------|
| `grix_invoke` | WS-based operations: send/delete messages, query contacts/sessions, manage groups, admin agents |
| `grix_auth` | HTTP auth: send email code, register, login, create/rotate agent API keys |
| `grix_card` | Generate Grix deep-link cards for conversations, profiles, and install status |
| `grix_egg` | Agent incubation: 7-step bootstrap (detect → install → create → bind → soul → gateway → accept) |

## License

MIT
