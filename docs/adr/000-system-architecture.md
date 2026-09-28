# ADR-000: 系统架构基线

- 状态：Accepted
- 日期：2026-07-08
- 决策者：tech-lead
- 影响范围：项目全栈（已上线 v0.1.2 的 brownfield macOS 应用）

> **更新注记（2026-08-13）：** 本 ADR 记录的是 v0.1.2 → v0.4.0 时期的架构决策。后续变更见 ADR-001（TLS MITM 抓包）、ADR-002（配置表示收敛与 API key 布尔契约）、ADR-003（Claude Code 环境变量契约）。当前版本以 `build.sh` 为准，模块归属见[模块地图](../agents/architecture.md)，菜单与设置导航见[界面约定](../agents/ui.md)；易过期信息以代码为准，本文不再同步版本号和行数。已删特性：tiktoken 依赖、long_context/background/think 路由场景（见 CONTEXT.md L87）。`proxy_runtime.py` 已不存在（`ProxyRuntime` 在 `proxy.py`）。

## 上下文

Magic Proxy 是一个**已存在**的 macOS 菜单栏应用：通过 SSH 隧道把远端 SOCKS5 代理暴露成本地 HTTP 代理，带实时流量统计。纯 Python 3 实现，可打包为原生 `.app` 并经 Developer ID 签名 + Apple 公证分发。本 ADR 首版基线为 v0.1.2；当前版本见 `build.sh`（易过期信息以代码为准）——v0.4.0 引入 Suanpan AI 路由网关 + Webview 配置界面 + 架构重构（详见 §v0.4.0 更新）；TLS MITM 抓包 feature 决策见 ADR-001。

AppGenesisForge（AGF）团队模板刚装入本项目，其 ADR-000 模板默认技术栈（React + FastAPI + Postgres）与 Magic Proxy 完全不符。本 ADR 是按**实际代码**（`grep` import + 通读各模块）回填的真实架构基线，替换模板示例。目的是让后续任何 feature / 技术选型有一个与代码一致的起点，并显式记录存量栈里**已知的风险**（见「备选方案 / 影响」段的 rumps 维护状态）。

核实方法：逐文件 `grep -nE 'import|from'` 确认依赖、`grep subprocess` 确认外部命令、通读 `build.sh` / `Magic Proxy.spec` / `build_dmg.sh` / `notarize.sh` 确认打包与分发链。凡查不到的（无版本锁定文件）一律标「未锁定 / 待确认」，不臆断。

## 决策

| 类别 | 选型 | 理由（经代码核实） |
|---|---|---|
| 项目形态 | macOS 菜单栏常驻代理工具，单平台 macOS | 已上线 v0.1.2 的存量应用；菜单栏图标常驻（🟢🟡🔴），后台跑 SSH 隧道 + HTTP→SOCKS5 代理 |
| 语言 | Python ≥3.9（自有代码）；打包工具链 ≥3.12 | `port_check.py:24,107` 用 `tuple[int, str]` 内建泛型订阅（PEP 585），需 3.9+；shebang `#!/usr/bin/env python3`；无 `python_requires` pin。**v0.3.0**：打包因 mitmproxy `requires-python>=3.12` 需构建解释器 ≥3.12（见 ADR-001 + 下「v0.3.0 更新」注） |
| 菜单栏 UI | `rumps`（基于 PyObjC） | `app.py:14 import rumps`；rumps 包裹 NSStatusItem/NSMenu/NSRunLoop，最小样板搭菜单栏 + Timer 每秒刷新；主线程跑 NSRunLoop，后台 daemon 线程跑 asyncio |
| 配置界面 | WKWebView（PyObjC）+ Web 配置服务 | `webview_window.py` 开 WKWebView 窗口加载自包含 `config_ui.html`（侧边栏分组：代理 / AI 路由）；`config_server.py`（`:9528`）提供 JSON CRUD + bearer token + 余额/用量查询。v0.4.0 取代已删的原生 `prefs.py` 配置窗。PyObjC 同时是 rumps 的传递依赖 |
| 代理核心 | `asyncio`（stdlib）HTTP→SOCKS5 | `proxy.py:2 import asyncio`；单事件循环多路复用，无 thread-per-connection；`proxy.py` 手写 SOCKS5 握手 + HTTP CONNECT 解析 |
| SSH 隧道 | 系统 `ssh` 子进程 + `sshpass`（密码认证） | `proxy.py:292 cmd=["sshpass","-d",str(r_fd),"ssh"]+args`；密码经**文件描述符**传给 sshpass（`proxy.py:283` 注释明确：「避免泄入 `ps`」）；key 认证走 `proxy.py:297 ["ssh","-i",key]` |
| 凭据存储 | macOS Keychain（`/usr/bin/security` CLI） | `keychain.py` 通篇 `subprocess.run(["security", ...])`；存为 generic-password，`SERVICE="com.magic-proxy"`，account key `f"{user}@{host}:{port}"`；提供 set/get/delete 三函数，被 `app.py` + `config.py` + `config_server.py` 依赖（高扇入） |
| 系统代理开关 | `networksetup`（subprocess） | `system_proxy.py` 调 `-setwebproxy` / `-setsecurewebproxy` / `-setproxybypassdomains` / `-setwebproxystate` / `-getwebproxy` / `-listallnetworkservices`；被 `app.py` 两处依赖（高扇入） |
| 端口占用检查 | `lsof` + POSIX kill（subprocess） | `port_check.py:65 ["lsof","-nP",f"-iTCP@127.0.0.1:{port}","-sTCP:LISTEN","-F","pcn"]`；解析输出拿 PID + 进程名，SIGTERM→SIGKILL 安全 kill；被 `app.py` 两处依赖（高扇入） |
| 外部终端启动（**已移除**） | —（原 `osascript` / `launcher.py`） | v0.3.0 前经 `osascript` + `launcher.py` 启动 iTerm2/Terminal.app；该功能连同 `launcher.py` + `tests/test_launcher.py` 已于 commit `8ff997a`（"删启动终端"）移除，此行仅留架构演进痕迹 |
| 图标生成 | Pillow | `generate_icon.py` 用 PIL 画菜单栏状态图标模板 `MenubarIcon.png`（纯 alpha 蒙版，运行时由 `menu_builder.py` 染色）；应用图标为 `icons/magic-ai-router-macos-v2.icns` 静态美术稿（build.sh `APP_ICON`），非代码生成 |
| 配置 | JSON 文件 `~/.magic-proxy.json` | 多隧道配置；默认配置由 `mpconf/config.py` 的 `DEFAULT_CONFIG` 生成（样例 `config.sample.json` 已删除——其 `http_listen` 旧字段格式易误导手编用户）；`auth_type` 为 `key`（默认）或 `password` |
| AI 路由网关（Suanpan） | FastAPI + uvicorn + httpx + pydantic | `suanpan/` 子包（`:9527`）：`main.py` app factory + `middleware.py`（APIKey + BodyLimit 中间件）；`router.py` 按 内联覆盖 → SUBAGENT-MODEL 标签 → 模型规则 → 默认 链路路由；`proxy.py` 流式转发 + SSE 用量提取；统一多家 LLM 为 Anthropic Messages API。经 `suanpan_runtime.py` 线程化宿主，**延迟导入**（依赖未装时 app 正常启动） |
| Suanpan 配置 | YAML 文件 `~/.suanpan.yaml` | 参考 `suanpan.example.yaml`；首次启动自动创建最小默认配置；Pydantic schema 在 `suanpan/config.py` |
| 打包 | PyInstaller（`--windowed` → `.app`） | `build.sh` 跑 `python3 -m PyInstaller --windowed --name "Magic Proxy" --osx-bundle-identifier com.benzai.magic-ai-router app.py`，各模块经 `--add-data` 打入；`Magic Proxy.spec` 是其固化产物（`hiddenimports=[]`） |
| .app 元数据 | PlistBuddy 注入 Info.plist | `build.sh` 用 `/usr/libexec/PlistBuddy` 设 `LSUIElement=true`（不显 Dock）+ `CFBundleShortVersionString` + `CFBundleVersion`（均 = `VERSION`） |
| 分发 | hdiutil DMG + codesign/notarytool/stapler 公证 | `build_dmg.sh` 用 `hdiutil` 打可拖拽安装 `.dmg`（含 Applications symlink）；`notarize.sh` 走 Developer ID 深签 + Hardened Runtime + `xcrun notarytool` 公证 + `xcrun stapler` 装订 + `spctl --assess` |
| 测试 | pytest（stdlib mock） | `tests/` 含 `test_port_check.py` / `test_system_proxy.py` 等；根目录有 `.pytest_cache` |

**线程模型（核实自 `app.py` + `proxy.py` + `suanpan_runtime.py`）：** 主线程跑 rumps NSRunLoop（菜单栏 UI）。后台 daemon 线程跑三个独立服务：① asyncio 事件循环（本地代理 `ProxyRuntime` :8888）；② Suanpan 网关（uvicorn :9527，延迟导入）；③ Web 配置服务（`config_server.py` :9528，`http.server`）。**Config server（:9528）与 AI 路由网关（:9527）是两个独立端口，不可合并。**`stats.py` 用 `threading.Lock` 保护跨线程流量统计读写；rumps.Timer 每秒触发菜单刷新。

**v0.3.0 更新 — TLS MITM 抓包 feature**（决策见 [ADR-001](001-mitm-packet-capture.md)，本节仅同步基线、不重述决策）：新增「抓包模式」经 mitmproxy 级联解密 HTTPS、抽 6 家 AI（OpenAI/Anthropic/DeepSeek/豆包/Qwen/MiniMax）请求·响应落 JSONL（`~/.magic-proxy-captures/<date>.jsonl`）；数据流 `浏览器 → mitmdump:8080（MITM 看明文）→ proxy.py:8888（upstream HTTP，零改动）→ SSH SOCKS5:1080 → 真实服务器`。新依赖 **mitmproxy 12.2.3**（`mitmdump` 子进程；PyInstaller 自带 hooks 打包，Task 1 spike 通过）。**打包工具链下界从 3.9 抬到 3.12**（mitmproxy ≥11.1.0 的 PyPI `requires-python>=3.12`；构建解释器约束、非用户系统约束——**自有代码下界仍 ≥3.9**）。新增 4 模块见「项目目录」的 `[v0.3.0/ADR-001]` 标注项。

**v0.4.0 更新 — Suanpan AI 路由网关 + Webview 配置 + 架构重构**：① 新增 Suanpan 网关（`suanpan/` 子包，FastAPI + uvicorn，:9527）将多家 LLM 后端统一为 Anthropic Messages API，按 内联覆盖 → `SUBAGENT-MODEL` 标签 → 模型规则 → 默认 链路路由；② 以 WKWebView + Web 配置服务（`config_server.py` :9528 + `config_ui.html` + `webview_window.py`）取代原生 `prefs.py` 配置窗（`prefs.py` 已删）；③ `app.py` 瘦身为纯编排器，拆出 `config.py` / `menu_builder.py` / `sys_proxy_controller.py` / `retry_scheduler.py` / `host_key_flow.py` / `subprocess_monitor.py`（`SSHMonitor`/`CaptureMonitor` 共同基类）/ `capture_store.py` 等模块；④ 新增 `sleep_blocker.py`（防睡眠）+ `login_item.py`（开机自启）。Suanpan 依赖为 `>=` 下界（非 `==` pin）。当前模块归属见[模块地图](../agents/architecture.md)，实现以代码为准。

## 备选方案

> 本项目是 brownfield，下列「决策」是回填既有实现时记录的可选项与否决理由，符合 ADR「至少 1 个备选 + 否决理由」要求。

- **A. 菜单栏 UI：rumps vs 原生 PyObjC NSStatusItem vs Swift/SwiftUI**
  - 选 rumps：纯 Python、几十行搭起状态栏 + 菜单 + Timer，与代理代码同语言零跨界。
  - 否决原生 PyObjC NSStatusItem：rumps 包裹的就是它，手写要重造 NSMenu/NSRunLoop 样板，无收益。
  - 否决 Swift/SwiftUI：会把单一代码库拆成双语言，代理逻辑（asyncio + SSH 子进程）重写代价远超菜单栏收益。**风险记录**：rumps 最后一次 PyPI 发布是 2022-10-14（v0.4.0），近 4 年无新版（见「版本与查证」表维护状态列）。当前仍可在 macOS 工作，属**需监控的存量风险**——若未来 rumps 在新 macOS 失效，回退路径是降级到直接用 PyObjC NSStatusItem（rumps 本身就是薄封装，迁移面可控）。

- **B. SSH 隧道：系统 ssh + sshpass vs paramiko / asyncssh（纯 Python SSH 库）**
  - 选系统 ssh 子进程：OpenSSH 是经过最广泛验证的 SSH 实现，处理 key agent / known_hosts / 压缩等边角情况最稳；本项目本质是「薄封装 + 状态栏」，不需要 SSH 协议层控制权。
  - 否决 paramiko / asyncssh：会重新实现 SSH 协议栈、引入大依赖、且端口转发 API 比直接 fork `ssh -L` 更复杂；系统 ssh 已被 v0.1.2 验证可靠。
  - 代价：依赖宿主机装 `sshpass`（密码认证）—— `brew install hudochenkov/sshpass/sshpass`，是用户侧手工依赖。

- **C. 凭据存储：Keychain（`security` CLI） vs PyObjC Security framework vs 明文 JSON**
  - 选 `security` CLI：`keychain.py` 用 subprocess 调 `/usr/bin/security`，无需额外 PyObjC framework binding，代码极简（~60 行）且走系统钥匙串加密存储。
  - 否决 PyObjC Sec framework：API 更繁琐，收益只是省一次进程 fork，不值。
  - 否决明文存 `~/.magic-proxy.json`：SSH 密码明文落盘是安全红线（触发 tech-lead 立即标记规则），不可取。

- **D. 打包：PyInstaller vs py2app vs Briefcase**
  - 选 PyInstaller：成熟、跨平台、能正确打包 PyObjC + rumps + asyncio 的原生扩展；`Magic Proxy.spec` 已固化配置。
  - 否决 py2app：macOS 专用、维护活跃度低于 PyInstaller、社区更小。
  - 否决 Briefcase（BeeWare）：定位是跨平台原生壳，对「纯菜单栏 + 子进程」场景过重。

## 影响

- **对现有代码**：本 ADR 是**现状记录**，不改任何代码；决策表逐条对应已存在的模块文件。
- **对团队**：新接手者须熟悉 rumps（小众）+ PyObjC（Cocoa 桥）+ asyncio 子进程模型；rumps 文档稀薄，主要靠读 `app.py` 现有用法。
- **对成本**：零外部服务成本——SSH 隧道走用户自有服务器，Keychain / networksetup / lsof 均为 macOS 内置，无 SaaS 依赖。Apple 公证消耗免费额度（Personal 团队有限，Developer Program 年费 $99 已由发布者承担）。
- **对运维 / 风险（重点）**：
  - ⚠️ **rumps 维护停滞**（最后发布 2022-10-14）—— 核心依赖，若新 macOS 破坏兼容需即时响应；回退见备选 A。
  - ✅ ~~无依赖锁定文件~~ **已解决（v0.4.0 `requirements-dev.txt` pin 核心依赖 `==`）**；Suanpan 网关依赖仍为 `>=` 下界（非 pin），未来可考虑收紧。
  - ⚠️ **`sshpass` 是外部 brew 依赖**——密码认证场景需用户手工安装。
  - 监控点：SSH 子进程崩溃、SOCKS5 端口未就绪、Keychain 读写失败、networksetup 改代理失败——均已有 logging，但无集中告警（超出菜单栏应用范围）。

## 项目目录

```
app.py                  # [v0.4.0] rumps 编排器：__init__ + _on_tick + 回调（入口）
config.py               # [v0.4.0] 配置 I/O（load/save/merge/migrate）
menu_builder.py         # [v0.4.0] 菜单栏 UI 构建 + 状态图标染色
proxy.py                # asyncio HTTP→SOCKS5 代理 + SSHMonitor + ProxyRuntime
subprocess_monitor.py   # [v0.4.0] 子进程生命周期基类（SSHMonitor / CaptureMonitor 继承）
sys_proxy_controller.py # [v0.4.0] 系统代理收敛状态机
retry_scheduler.py      # [v0.4.0] SSH 重试退避调度
host_key_flow.py        # [v0.4.0] SSH 主机密钥信任流程
host_key.py             # SSH known_hosts 管理
port_check.py           # lsof 查端口占用 + SIGTERM→SIGKILL 安全 kill
config_server.py        # [v0.4.0] Web 配置服务 :9528（JSON CRUD + bearer token + 余额/用量）
config_ui.html          # [v0.4.0] 自包含 Web 配置面板（侧边栏导航）
webview_window.py       # [v0.4.0] WKWebView 窗口
suanpan_runtime.py      # [v0.4.0] Suanpan 网关线程化运行时（延迟导入）
suanpan/                # [v0.4.0] AI 路由网关子包（FastAPI :9527，详见 docs/agents/architecture.md）
capture.py              # [v0.3.0/ADR-001] mitmdump 子进程管理（抓包模式）
capture_store.py        # [v0.4.0] 抓包目录/文件管理（跨进程共享）
ai_capture_addon.py     # [v0.3.0/ADR-001] mitmproxy addon：6 家 AI 请求·响应抽取落 JSONL
ca_trust.py             # [v0.3.0/ADR-001] 根 CA 信任检测 + 首次 PyObjC 引导窗
mitmdump_entry.py       # [v0.3.0/ADR-001] PyInstaller 冻结 .app 内 mitmdump 入口 shim
system_proxy.py         # networksetup 包装：事务式 apply/clear + 崩溃恢复
sleep_blocker.py        # [v0.3.7] 防系统睡眠（代理运行期）
login_item.py           # [v0.3.7] macOS 登录项（开机自启）
chromium_proxy.py       # 单 Chromium 应用代理（--proxy-server 启动参数）
log_window.py           # PyObjC 实时日志窗
stats.py                # 线程安全流量统计 + 速率计算（threading.Lock）
keychain.py             # macOS Keychain 封装（/usr/bin/security）—— SSH 密码存取
generate_icon.py        # Pillow 生成菜单栏状态图标模板 MenubarIcon.png
build.sh                # PyInstaller 打包（VERSION=0.4.0；requirements-dev.txt）
build_dmg.sh            # hdiutil 打可拖拽安装 .dmg
notarize.sh             # codesign + notarytool + stapler + spctl 公证链
config.sample.json      # Magic Proxy 配置模板（已删除——默认配置改由 DEFAULT_CONFIG 生成）
suanpan.example.yaml    # [v0.4.0] Suanpan 配置模板
tests/                  # pytest 测试套（数量以 `pytest tests/` 为准）
~/.magic-proxy.json     # 运行时配置（多隧道）
~/.suanpan.yaml         # [v0.4.0] Suanpan 运行时配置
```

## 本地开发流程

> Magic Proxy 是已运行项目，无 Makefile / docker-compose；流程直接照搬 `CLAUDE.md` 「命令」段（经核实与 `build.sh` 一致）。

1. **开发模式**：`python3 app.py`（需 macOS，因依赖 rumps / PyObjC / networksetup 等）
2. **装依赖**：`pip3 install rumps`（rumps 传递带入 PyObjC）。打包另需 `pip3 install pyinstaller`，密码认证另需 `brew install hudochenkov/sshpass/sshpass`
3. **打包 .app**：`bash build.sh` → `cp -R "dist/Magic Proxy.app" /Applications/`
4. **打分发包**（可选）：`bash build_dmg.sh`（先跑 build.sh）；`bash notarize.sh`（需 Developer ID + Apple ID 凭证）

## 本 ADR 不覆盖的决策

- **CI/CD pipeline**（lint/test/build/notarize 自动化）—— 当前手工跑 `build.sh` / `notarize.sh`
- **自动更新机制**（Sparkle / 自更新）—— 当前无，用户手工替换 .app
- **崩溃上报 / 遥测**—— 无
- **跨平台（Windows / Linux）**—— 当前 macOS 单平台，菜单栏 + Keychain + networksetup 均为 macOS 专属
- **Python 解释器版本上界 / 解释器 pinning**—— 仅写下界 ≥3.9，未 pin 具体发行版
- **测试框架与覆盖率门禁**—— pytest 已 pin（`requirements-dev.txt` `pytest==8.4.2`）；无覆盖率指标门禁
- **rumps 长期替代方案**（迁移到原生 PyObjC 或 Swift）—— 当前监控，触发条件见「影响」风险点
- **依赖锁定文件格式选型**（requirements.txt vs pyproject.toml vs uv.lock）—— 留给首个落地任务

## 后续工作

- [x] ~~tech-lead / backend-dev：创建依赖锁定文件~~ **✅ 已完成（v0.4.0 `requirements-dev.txt` pin 核心依赖 `==`；Suanpan 依赖 `>=` 下界）；版本表已回填**
- [ ] tech-lead：监控 rumps 在新 macOS 版本的兼容性；一旦失效，按「备选方案 A」降级到原生 PyObjC NSStatusItem，届时另开 ADR
- [ ] product-lead：首个 feature 派工时把本 ADR 技术约束（macOS 单平台 / rumps / asyncio 子进程模型）附带给开发者

## 版本与查证

> tech-lead 行事原则 #3「先查最新版再决策」的回填段。**v0.4.0 起 `requirements-dev.txt` 已 pin 核心依赖（`==`）**；Suanpan 依赖为 `>=` 下界（非 pin）。「选定版本」列反映 pin 值。「最新稳定版」列仍为 2026-07-08 查证基线（本次 re-baseline 未重查），仅供参考。

**查证基线日期**：2026-07-08

| 选型 | 选定版本 | 最新稳定版 | 与最新版差距 | 维护状态 | 信息来源（含原文摘录） |
|---|---|---|---|---|---|
| Python | 未 pin（自有代码 ≥3.9；**打包工具链 ≥3.12**，v0.3.0 起） | — | — | — | 代码内证（无 `python_requires`）；打包下界 3.12 因 mitmproxy `requires-python>=3.12`（ADR-001 §版本与查证 实测 2026-07-08） |
| mitmproxy（v0.3.0，抓包） | 12.2.3（`requirements-dev.txt` `==`） | 12.2.3（2026-05-12） | 取最新 | Active | 决策 + 查证见 [ADR-001](001-mitm-packet-capture.md)；`requirements-dev.txt` `mitmproxy==12.2.3`；PyInstaller 自带 hooks（`mitmproxy/utils/pyinstaller/`）打包，Task 1 spike 通过 |
| rumps | `requirements-dev.txt` `rumps==0.4.0` | 0.4.0（2022-10-14） | 无（已 pin 到基线最新，但「最新」本身就旧） | ⚠️ **Stale**——最后一次 PyPI 发布 2022-10-14，近 4 年无新版；GitHub 偶有 commit 但无正式 release。当前仍可在 macOS 工作，属需监控的存量风险 | [pypi.org/project/rumps](https://pypi.org/project/rumps/) — "Latest release. Released: Oct 14, 2022"；[github.com/jaredks/rumps](https://github.com/jaredks/rumps) |
| PyObjC（pyobjc-core / pyobjc-framework-Cocoa） | `requirements-dev.txt` `==12.2.1` | 12.2.1（2026-06-19） | 无（已 pin 到基线最新） | Active | [pypi.org/project/pyobjc-core](https://pypi.org/project/pyobjc-core/) — "12.2.1 Jun 19, 2026; 12.2 May 30, 2026; 12.1 Nov 14, 2025"；注：`webview_window.py`/`ca_trust.py`/`log_window.py` 直接用 objc/AppKit/Foundation，且 rumps 传递依赖 PyObjC |
| PyInstaller | `requirements-dev.txt` `pyinstaller==6.21.0` | 6.21.0 | 无（已 pin 到基线最新） | Active | [pyinstaller.org](https://pyinstaller.org/) — "PyInstaller 6.21.0 documentation"；[github.com/pyinstaller/pyinstaller/releases](https://github.com/pyinstaller/pyinstaller/releases) |
| Pillow | `requirements-dev.txt` `Pillow==12.3.0` | 12.3.0（2026-07-01） | 无（已 pin 到基线最新） | Active（季度发布：1/4/7/10 月） | [pypi.org/project/pillow](https://pypi.org/project/pillow/) — "Latest release. Released: Jul 1, 2026"；[pillow.readthedocs.io](https://pillow.readthedocs.io/) |
| pytest | `requirements-dev.txt` `pytest==8.4.2` | 待确认（基线未查） | — | Active | `requirements-dev.txt` pin；`tests/` 由 `pytest tests/` 运行 + 根目录 `.pytest_cache` |
| ruff | `requirements-dev.txt` `ruff==0.12.2` | 待确认（基线未查） | — | Active | lint 工具（`requirements-dev.txt` pin；`.ruff_cache`） |
| **Suanpan 网关依赖**（v0.4.0，`>=` 下界**非 pin**） | — | — | — | — | 见 `requirements-dev.txt` §Suanpan AI router gateway；延迟导入，未装时 app 正常启动 |
| fastapi | `requirements-dev.txt` `>=0.115` | 待确认（基线未查） | — | Active | Anthropic Messages API 兼容后端统一层 |
| uvicorn[standard] | `requirements-dev.txt` `>=0.30` | 待确认（基线未查） | — | Active | Suanpan 网关 ASGI server（:9527） |
| httpx[http2] | `requirements-dev.txt` `>=0.27` | 待确认（基线未查） | — | Active | 流式代理转发后端 LLM 请求 |
| pydantic | `requirements-dev.txt` `>=2.7` | 待确认（基线未查） | — | Active | Suanpan 配置 schema（`suanpan/config.py`） |

**回填规则**：执行层在创建依赖锁定文件（write `requirements.txt` / `pyproject.toml` / `uv.lock`）时，把实际 pin 的版本回填本表「选定版本」列，commit message 加 `docs(adr): backfill ADR-000 verification for [pkg]`，无需新开 ADR。选定版本与最新稳定版出现 ≥1 个 minor/major 差距时，必须在「与最新版差距」列写明原因与复盘条件（遵循 tech-lead 行事原则 #3）。
