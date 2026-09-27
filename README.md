# Magic Stack — macOS AI Gateway, SSH Tunnels & Traffic Capture

[English](README.md) · [简体中文](README.zh-CN.md)

**Magic Stack is an open-source macOS menu bar app for routing AI coding agents, managing SSH connections, and inspecting AI API traffic.** Connect Claude Code, Codex, OpenCode and ZCode to your own model providers through a local LLM gateway. Manage an HTTP/SOCKS5 proxy, SSH port forwarding and NFS remote mounts from the same app, with English and Simplified Chinese interfaces.

**[Download for macOS](https://github.com/benz-ai-x/magic-stack/releases/latest)** · [Quick start](#quick-start-connect-your-ai-coding-agent) · [Docker deployment](#docker-self-hosted-ai-gateway) · [FAQ](#faq)

[![Release](https://img.shields.io/github/v/release/benz-ai-x/magic-stack)](https://github.com/benz-ai-x/magic-stack/releases)
[![CI](https://github.com/benz-ai-x/magic-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/benz-ai-x/magic-stack/actions/workflows/ci.yml)
![Platform: macOS Apple Silicon](https://img.shields.io/badge/macOS-Apple%20Silicon-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Magic Stack macOS settings: SSH server list, connection settings, port forwarding and NFS tabs](assets/docs/settings-servers-en-v0140.png)

## What can you do with Magic Stack?

Magic Stack brings together two independent products: **Suanpan (算盘)**, the AI routing gateway, and **Magic Proxy**, the SSH proxy and traffic capture tools. You can use AI routing without an SSH server, or use the networking tools without configuring any AI provider.

| Your workflow | Magic Stack capability |
|---|---|
| Use different LLMs in your coding agents | Route requests by model prefix, explicit `provider/model` target or a default route; configure Claude Code roles separately |
| Connect several agents to one gateway | Preview and apply settings for Claude Code, Codex, OpenCode and ZCode; agents receive a local gateway token |
| Reach services through your own server | HTTP proxy over SSH SOCKS5, parallel SSH local port forwards, automatic retries and wake-triggered reconnects |
| Work with remote files on a Mac | NFSv4 mounts over SSH, remote setup assistance and automatic remounting after connection recovery |
| Understand AI usage | Local request, token, cache and error statistics, plus provider balance and quota queries where supported |
| Inspect AI requests and responses | Optional TLS capture with mitmproxy and local JSONL output for recognized AI API calls |

Explore [agent compatibility](#ai-gateway-agent-and-protocol-compatibility), [SSH and NFS](#ssh-proxy-port-forwarding-and-nfs-mounts), [usage and capture](#ai-usage-statistics-and-traffic-capture), or [data storage](#local-ports-data-and-credentials).

## Quick start: connect your AI coding agent

### Install the macOS app

1. Download the Apple Silicon `.dmg` from [GitHub Releases](https://github.com/benz-ai-x/magic-stack/releases/latest).
2. Drag **Magic Stack.app** into **Applications** and open it. The app lives in the menu bar. To switch to English, use **选项 → 语言 → English** in its menu.
3. Open **Preferences → AI Router → Quick Start**. Select a provider and enter its API key.
4. Run the endpoint probe, review the available models, and select the installed agents you want to configure. Review the configuration preview before applying it.
5. Check the default model and any role mappings under **Claude Code Sync**, start AI routing from the menu bar if it is stopped, then restart your agent to load its new settings.

You need an installed coding agent and credentials for your chosen model provider. SSH access is only needed for the networking features. Model and protocol availability depends on the provider, endpoint and account plan; the compatibility table below explains the gateway's supported paths.

The setup engine writes the gateway address and a **local client token** into agent configuration. Provider API keys stay in the gateway configuration and are used to authenticate outbound requests to the selected provider.

### Configure Magic Stack with an AI assistant

Click **Copy AI Assistant Instructions** in settings and give the copied instructions to your assistant. They include access to the authenticated local configuration API, allowing the assistant to inspect and update app settings. The [agent API guide](docs/agent.md) documents the available operations.

## AI gateway: agent and protocol compatibility

**Suanpan is Magic Stack's local LLM routing gateway**, normally at `http://127.0.0.1:9527`. It accepts Anthropic Messages, OpenAI Chat Completions and OpenAI Responses requests. Matching protocols use passthrough; Anthropic Messages can also be translated to an OpenAI Chat backend, including streaming responses and tool calls.

| Agent or client | Gateway endpoint | Supported upstream path |
|---|---|---|
| Claude Code, ZCode, Anthropic Messages clients | `POST /v1/messages` | Anthropic-compatible endpoint, or translation to an OpenAI Chat endpoint |
| OpenCode | `POST /v1/messages` or `POST /v1/chat/completions` | Choose the client protocol to match the configured provider path |
| OpenAI Chat-compatible clients | `POST /v1/chat/completions` | Provider configured with `protocol: openai` |
| Codex, OpenAI Responses clients | `POST /v1/responses` | A working native Responses endpoint configured as `responses_base_url` |

**Responses-to-Anthropic/Chat conversion is not implemented.** Codex therefore requires an upstream Responses endpoint. Chat requests also require an OpenAI Chat upstream; they are not converted to Anthropic. See the [request handlers](suanpan/main.py) and [protocol design](docs/adr/010-protocol-matrix-and-agent-setup.md).

Built-in provider presets include **GLM (Zhipu AI), DeepSeek, Kimi, OpenAI, Anthropic, OpenRouter, Qwen, SiliconFlow, MiniMax and Volcengine Ark**. You can also configure custom compatible endpoints. Presets help select URLs and authentication; they do not guarantee every protocol or model is available on every account. The [provider registry](shared/provider_auth.py) defines the presets.

### Route Claude Code models by role or prefix

Use **Claude Code Sync** to map Opus, Sonnet, Haiku and Subagent roles to provider models. Saving explicit role mappings also updates the gateway's tier rules, keeping agent settings and routing aligned.

For manual configuration, this YAML **fragment** illustrates prefix routing. Replace `primary` and `economy` with names already defined in your `providers` section, and replace the example model IDs with models available from those providers:

```yaml
router:
  default: primary/main-model

rules:
  - match_prefix: claude-opus
    route_to: primary/main-model
  - match_prefix: claude-sonnet
    route_to: primary/main-model
  - match_prefix: claude-haiku
    route_to: economy/small-model
```

Rules are checked in order; the first matching prefix wins. An explicit `provider/model` in the request takes priority, followed by a `SUBAGENT-MODEL` system tag, then prefix rules and the default route. See the [configuration example](docs/examples/suanpan.example.yaml) and [routing implementation](suanpan/router.py).

Prompt caching depends on the upstream protocol and provider. The `anthropic_native` option preserves Anthropic cache markers on compatible endpoints; Anthropic-to-Chat conversion drops `cache_control` because Chat has no equivalent field. See the [prompt caching design](docs/adr/004-prompt-caching-and-prefix-stability.md).

## SSH proxy, port forwarding and NFS mounts

**Magic Proxy manages connections to SSH servers you control.** Add a server once under **Preferences → Proxy → Servers**, then use its connection, port forwarding and NFS tabs.

- **HTTP/HTTPS proxy over SSH:** choose a proxy server and start the connection. Applications use the local HTTP proxy on `127.0.0.1:8888`; SSH provides the SOCKS5 upstream on `127.0.0.1:1080` by default.
- **System or application proxy:** enable macOS system proxy integration, or launch supported Chromium applications with an application-specific proxy. System proxy settings are restored when disconnected.
- **Parallel SSH port forwarding:** map a local port to a service reachable from the remote server, such as a development web server or database. Multiple server sessions can run together, with per-forward toggles and connection tests.
- **NFSv4 over SSH:** mount remote directories on macOS using its built-in NFS client. Setup can install and configure NFS on supported Linux servers; remote setup and initial local mount authorization require administrator privileges.
- **Connection recovery:** active SSH sessions retry with backoff and reconnect on wake. NFS mounts are forcibly unmounted on disconnection and configured automatic mounts are restored after recovery.

For a tool that supports HTTP proxies, after connecting Magic Proxy:

```bash
curl --proxy http://127.0.0.1:8888 https://example.com
```

![Magic Stack SSH port forwarding: local-to-remote port mappings with individual enable switches and connection tests](assets/docs/portforwards-en-v0140.png)

## AI usage statistics and traffic capture

### Track tokens, cache usage and provider balances

The **Usage Stats** page reads the gateway's local usage log. It shows request counts, input/output tokens, cache reads and writes, errors and overall average latency, with breakdowns by provider, routing source, detected agent and day. Agent attribution comes from the request's User-Agent; unidentified clients remain in an unknown group.

The **Balances** page separately queries supported provider APIs, including integrations for DeepSeek, GLM and Kimi. Available balances, quotas and reset times depend on the account and provider. Token statistics are not an invoice or a universal per-request cost calculator.

![Magic Stack AI usage dashboard: request counts, input and output tokens, cache hit rate, average latency and daily usage](assets/docs/usage-stats-en-v0140.png)

### Inspect AI API traffic with TLS capture

Capture mode is an **experimental, optional** mitmproxy integration. It can extract prompts and responses from recognized **OpenAI, Anthropic, DeepSeek, Doubao, Qwen and MiniMax** API endpoints into local JSONL files.

Traffic must pass through the capture proxy to be inspected; enabling the AI gateway alone does not capture its outbound traffic. First use requires trusting the local root CA. HTTPS traffic routed through the capture proxy is decrypted for forwarding, while only recognized AI API calls are recorded. Capture files can contain prompt and response text; multimodal content is represented with placeholders. See the [capture implementation](capture/ai_capture_addon.py) for endpoint coverage.

## Docker: self-hosted AI gateway

Run **Suanpan and the web configuration UI** on Linux or a Docker host without the macOS app. Install Docker with Compose, then build and start the service from this repository:

```bash
git clone https://github.com/benz-ai-x/magic-stack.git
cd magic-stack
bash docker/suanpan.sh up
bash docker/suanpan.sh config-ui
```

Open `http://127.0.0.1:9528/` on the Docker host and log in with the token printed by `config-ui`. Configure your providers and default route before connecting an agent.

```bash
# Check that the gateway is running; this does not test provider credentials.
curl http://127.0.0.1:9527/health

# Preview, then apply Claude Code settings on the Docker host.
bash docker/suanpan.sh sync --dry-run
bash docker/suanpan.sh sync
```

The Compose file publishes ports `9527` and `9528` on host loopback only and persists configuration in `docker/data/`. The `sync` command updates the host's `~/.claude/settings.json` through a mounted directory. Docker includes the gateway and web configuration; SSH management, NFS mounts, TLS capture and the menu bar require the macOS app. See the [Docker deployment guide](docs/docker-deploy.md) for remote access and deployment details.

## Local ports, data and credentials

| Default local port | Purpose |
|---|---|
| `9527` | Suanpan AI gateway |
| `9528` | Web settings and authenticated configuration API |
| `8888` | Magic Proxy HTTP/HTTPS proxy |
| `1080` | SSH SOCKS5 proxy |
| `8080` | Optional TLS capture proxy |

The macOS services bind to loopback. The configuration service on `9528` starts when settings are open, stays available after **Copy AI Assistant Instructions** until app exit, or can be kept on with the configuration API setting. In Docker, the configuration service stays running and host exposure is controlled by the [Compose port mappings](docker/compose.yml).

| Data | Default macOS location or handling |
|---|---|
| Server, proxy and app settings | `~/.magic-proxy.json` |
| Provider keys and routing rules | `~/.suanpan.yaml`; provider keys can also reference environment variables |
| SSH and remote sudo passwords | macOS Keychain |
| Gateway usage metadata | `~/.suanpan/logs/usage.jsonl` |
| Captured AI request/response content | `~/.magic-proxy-captures/` |

**Local-first means you control the gateway and its configuration.** AI requests and provider credentials are sent to the upstream provider you select; no Magic Stack-hosted relay is required. Agent setup uses a separate local token, which is removed before upstream forwarding. Configuration files are written atomically with `0600` permissions, and saved provider keys are masked in the settings API. See [credential handling](shared/provider_auth.py) and [configuration storage](shared/config_store.py).

## FAQ

### Can I use Claude Code with GLM, DeepSeek, Kimi or Qwen?

Yes, through a provider endpoint supported by Suanpan: an Anthropic-compatible endpoint or an OpenAI Chat endpoint with translation. Configure the provider and model, then use Claude Code Sync. Model quality, tool behavior and caching support depend on the selected upstream model and protocol.

### Can Codex use every provider preset?

No. Codex connects through `/v1/responses`, so the provider must expose a working native Responses endpoint configured as `responses_base_url`. A provider that only offers Chat Completions or Anthropic Messages is insufficient for the current Codex path.

### Do I need an SSH server to use the AI gateway?

No. Suanpan and Magic Proxy are independent. AI routing can run without SSH, and SSH port forwarding or NFS mounts can run without AI routing. Gateway traffic is not automatically sent through the SSH tunnel.

### Does Magic Stack run models locally?

Magic Stack routes requests; it does not run model inference. You can configure a reachable local or self-hosted API server if it exposes a supported protocol and endpoint layout.

### Is Magic Stack a VPN client?

Magic Stack provides HTTP/SOCKS5 proxying and SSH port forwarding. Applications must use the proxy or forwarded ports. The OpenVPN tab currently provides service detection and a placeholder; it does not manage VPN connections.

### Is Magic Stack free, and does it include model access?

Magic Stack is free and open source under the MIT license. Bring your own provider accounts and SSH servers; their API, subscription and hosting charges are separate.

### Is Magic Stack the same project as Magic AI Router?

Yes. Magic AI Router (also written Magic-AI-Router) was renamed Magic Stack to reflect its networking, routing and capture features. Magic Proxy and Suanpan remain its two product components, and existing configuration paths are retained. See the [rename release notes](CHANGELOG.md).

### Can I use it on Linux or Windows?

The desktop application targets macOS on Apple Silicon. Suanpan can be self-hosted with Docker; there is no native Linux or Windows desktop app in this repository. See the [Docker guide](docs/docker-deploy.md) for the supported headless workflow.

## Develop and contribute

For the full macOS development dependency set, use **Python 3.12+**. The bundled mitmproxy toolchain requires it.

```bash
git clone https://github.com/benz-ai-x/magic-stack.git
cd magic-stack
python3.12 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 app.py
```

Run the Python and settings-UI checks:

```bash
python3 -m pytest tests/ --cov
node --test tests/js/*.test.mjs
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution conventions, [build.sh](build.sh) for app packaging and the version source, and [scripts/notarize.sh](scripts/notarize.sh) for the signing and notarization workflow.

| Documentation | Contents |
|---|---|
| [Changelog](CHANGELOG.md) | Release history and changes |
| [Configuration example](docs/examples/suanpan.example.yaml) | Gateway YAML fields and routing examples |
| [Agent API guide](docs/agent.md) | Authenticated app configuration API |
| [Domain glossary](CONTEXT.md) | Product boundaries, routing priority and lifecycle behavior |
| [Architecture decisions](docs/adr/) | Protocol compatibility, credentials, capture, NFS and UI design |

Bug reports and contributions are welcome in English or Chinese: [Issues](https://github.com/benz-ai-x/magic-stack/issues) · [Discussions](https://github.com/benz-ai-x/magic-stack/discussions).

## License

[MIT](LICENSE) — Copyright (c) 2026 benz-ai-x.
