# CLAUDE.md

## 项目概览

Magic Stack 是面向开发者的 macOS 菜单栏应用，把远程网络连接与 AI 编程工具的模型接入放在同一个入口，承载两个独立产品：

- **Magic Proxy（网络工具）**：通过 SSH 提供 HTTP→SOCKS5 代理，并管理 OpenVPN 接入、SSH 端口映射、NFS 远程挂载及 TLS 抓包，解决访问远端服务、使用远程文件和检查 AI API 流量的问题。
- **Suanpan / 算盘（AI 路由网关）**：让 Claude Code、Codex 等客户端通过统一网关访问多家模型供应商；接收 Anthropic Messages、OpenAI Chat 与 Responses 请求，按路由规则选择后端，并提供客户端配置同步、用量统计与余额查询。

两个产品可独立使用：AI 路由无需 SSH 服务器，网络功能无需配置模型供应商。SSH 与 VPN 在接入层互斥，端口映射与 NFS 服务独立管理。

用户从菜单栏启停服务，在偏好设置中配置服务器、供应商和路由；设置页也支持浏览器访问。代码以 Python 为主：`app.py` 装配 macOS 壳，rumps/PyObjC 承载菜单与窗口，设置页使用 HTML/JS；Suanpan 使用 FastAPI/uvicorn，网关与配置服务还支持 Docker 部署。

用户可见名用 **Magic Stack**；Magic Proxy / Magic-AI-Router 也见于内部代码和历史文档。

模块清单、菜单结构与依赖等易过期信息**以代码为准**；版本号见 `build.sh`，依赖见 `requirements*.txt`。

## 按任务读取

- **领域探索、术语或架构决策**：先读 [领域文档约定](docs/agents/domain.md)，再读根目录 `CONTEXT.md` 与相关 `docs/adr/`。
- **模块定位、职责划分、导入边或配置持久化变更**：读 [模块地图](docs/agents/architecture.md)，找到已有归宿后再修改。
- **菜单、设置窗或原生桥接变更**：读 [界面约定](docs/agents/ui.md)，覆盖导航、状态表达和浏览器降级。
- **Issue / PR 操作或 wayfinder 地图**：读 [GitHub 工作流](docs/agents/issue-tracker.md)，使用 `gh` CLI。
- **分流或标签操作**：读 [五标签映射](docs/agents/triage-labels.md)。

## 命令

```bash
# 安装依赖 / 开发运行
pip3 install -r requirements-dev.txt
python3 app.py

# Python 覆盖率 / 设置窗 JS 测试
python3 -m pytest --cov tests/
node --test tests/js/*.test.mjs

# 打包 / 安装到本机
bash build.sh
cp -R "dist/Magic Stack.app" /Applications/

# 签名与公证分发
bash scripts/notarize.sh
```

本机测试用 `python3 -m pytest`：bare `pytest` 可能落到 Python 3.9，无法解析 Suanpan 的类型注解。Node 用文件 glob，Node ≥26 的目录模式会报 `MODULE_NOT_FOUND`。覆盖率排除项见 `.coveragerc`，数字以当次运行为准。

## 架构与兼容约束

- **单一归属**：每项职责由一个模块持有，调用方复用。新增或移动模块时更新模块地图；`tests/test_docs_drift.py` 校验逐域清单和设置导航。
- **导入方向**：层次由低到高为 `shared/` 与 `util.py` → 域 → `services/` / `shellui/` → `app.py` / `docker/`。导入只许向下，同层只许同域；例外以 `tests/test_arch_imports.py` 的 `_ALLOWED_SAME_LAYER` 为准。
- **运行形态**：Config server（默认 :9528）与 Suanpan 网关（默认 :9527）是独立服务，不可合并。网关依赖延迟导入，缺席时壳仍正常启动并提示安装。
- **Python 下界**：自有代码保持 ≥3.9；打包解释器须 ≥3.12（mitmproxy 工具链，见 ADR-001）。
- **PyObjC**：方法名不能以单下划线开头，会被当成 ObjC selector。

## 校验与文案

- **校验镜像**：设置窗 JS `validateConfig` 与 Python 分域校验器双层拦截。修改规则时同步两侧；新增单侧规则须在 `tests/test_validation_mirror.py` 的白名单登记。该测试通过 `tests/js/validate_mirror.mjs` 检查「同错同净」（Node 缺席时跳过）。
- **国际化**（ADR-012）：用户可见新文案同时补齐 `shared/locales/*.json` 双语 catalog；Python 用 `i18n.t("字面键")`，JS 用 `tt("字面键")`，禁止动态键。日志与注释可直接写中文。`tests/test_i18n.py` 校验键与占位符、英文翻译、取词和中英文渲染残留；未迁移 Python 文件在 `_HAN_WHITELIST` 登记。设置窗 LAYER 1 的 zh 标签/复合文案是 Node 测试钉住的数据面，渲染侧统一按键取词。
