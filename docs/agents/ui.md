# 界面约定

修改菜单、设置窗或桥接时使用。菜单实现以 `shellui/menu_builder.py` 为准；状态语法见 [CONTEXT.md](../../CONTEXT.md)「菜单状态语法」，校验与文案规则见 [CLAUDE.md](../../CLAUDE.md)。

## 菜单与图标

菜单按状态 → 接入 → 功能 → AI → 应用分组。SSH/VPN 接入行点击切换连/断，对端活跃时确认切换；转发与 NFS 服务独立于接入，只有「经代理启动」随 SSH 接入置灰。防睡眠与登录启动在设置窗。

菜单栏主图标：灰=无连接、蓝=SSH 已连接、绿=VPN 已连接、黄=连接中或暂停；资源在 `assets/MenubarIcon*.png`，由 `tools/generate_icon.py` 生成。菜单内状态圆点使用手绘位图；功能项图标用 SF Symbols，旧系统静默降级。`.app` 的 `LSUIElement=true` 保持 Dock 隐藏。

流量只在展开菜单后显示，顶部保持状态图标。SSH/VPN 已连接时，状态区显示来源、↓ 下载 / ↑ 上传速率与累计量；暂停、重连、断开时隐藏。SSH 统计本地 HTTP/HTTPS 代理转发字节，累计自应用启动（不含直连 SOCKS5、独立转发/NFS）；VPN 使用管理口字节计数，累计自本次连接启动（含自动重连），不显示 HTTP 代理连接数。速率变化就地刷新，不重建菜单。

## 设置窗与浏览器

**偏好设置：** 菜单打开 WKWebView（`http://127.0.0.1:9528/`）。侧边栏为代理（服务器（连接/端口映射/NFS/OpenVPN tab）/ 网络设置）+ AI 路由（快速接入 / 供应商 / Claude Code 同步 / 运行统计 / 余额速览）+ 系统（系统选项）。

该导航由 `tests/test_docs_drift.py` 与设置窗 VIEWS 注册表核对，增删页面时同步更新。

同一页面支持浏览器直接访问（token 登录）。原生 bridge 不可用时，重连/转发启停给 toast 提示；「复制 AI 助手指令」回退到经认证的 `GET /api/agent-instructions`。原生复制经 `agentInstructionsCopied` 回传成功/失败并显示 toast，指令正文与 token 不经回传进入 JS。协议归 `shellui/bridge_protocol.py`，`webview_window.py` 保持 ObjC 薄 adapter。

配置服务启停或浏览器兼容变更前读 [ADR-009](../adr/009-config-ui-web-and-port-lifecycle.md)；保存与 dirty 变更前读 [CONTEXT.md](../../CONTEXT.md)「设置窗桥接」「保存流」「服务生命周期」。配置服务启停经生命周期编排。
