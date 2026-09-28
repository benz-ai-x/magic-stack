"""Docker 容器入口（issue #22）：Suanpan 网关的 Linux 部署形态.

两种模式：
- ``serve``（默认）：首启引导 /data/suanpan.yaml → create_app →
  uvicorn 绑 0.0.0.0:<listen_port>（绕开 run_from_config_path 的回环
  守卫：容器内绑 0.0.0.0 是 Docker 标准姿势，信任边界移到宿主机端口
  映射——compose 固定 ``127.0.0.1:<port>:<port>``，宿主机侧仅回环可达）。
- ``sync-claude-code`` [--dry-run]：重定向 config_store.PATHS 三键
  （sp→/data/suanpan.yaml、mp→/data/magic-proxy.json、claude_settings→
  /host-claude/settings.json）后整体复用 services.claude_code_setup 的
  setup()/preview()。mp 键也必须重定向——#9 之后 AUTH_TOKEN 写的是本地
  客户端 token（mpconf/local_token，存 PATHS["mp"]），落在 /data 卷才能
  跨容器重建保持稳定。容器内 suanpan_listen() 算出的
  http://127.0.0.1:<port> 恰等于宿主机侧 Claude Code 应使用的地址。

Linux 容器内 PyObjC 的 ``Security`` 不存在：``shared/keychain`` 自身
try/except 可选化（Security=None + 全吞异常兜底），import 链裸奔即可，
无需任何 stub。
"""
from __future__ import annotations

import json
import os
import sys
import time

from services.suanpan_runtime import SuanpanRuntime
from services.gateway_watchdog import GatewayWatchdog
from mpconf.config_state import recover_pending_txn


def default_paths(env=None):
    """三条路径的容器默认值（env 可覆盖，便于本地调试与测试）。

    ADR-010 M4：Agent 配置目标文件同模式（env 可覆盖；compose 默认不
    挂载 ~/.codex 等——需要容器内配置 Agent 的用户自行加卷）。
    """
    env = os.environ if env is None else env
    data_dir = env.get("SUANPAN_DATA_DIR", "/data")
    return {
        "sp": os.path.join(data_dir, "suanpan.yaml"),
        "mp": os.path.join(data_dir, "magic-proxy.json"),
        "claude_settings": env.get(
            "CLAUDE_SETTINGS_PATH", "/host-claude/settings.json"),
        "codex_config": env.get("CODEX_CONFIG_PATH", "/host-codex/config.toml"),
        "opencode_config": env.get(
            "OPENCODE_CONFIG_PATH", "/host-opencode/opencode.json"),
        "zcode_config": env.get("ZCODE_CONFIG_PATH", "/host-zcode/config.json"),
    }


def bootstrap_default_config(sp_path: str, data_dir: str) -> bool:
    """首启引导：/data/suanpan.yaml 缺失时生成最小默认配置。

    用量日志指向 data 卷（容器重建不丢）；usage_log 自身不建目录，
    logs/ 在此建好。经 config_store.atomic_write（0600 + 原子替换）。
    已存在则不动（返回 False）——用户配置永不被覆盖。
    """
    if os.path.exists(sp_path):
        return False
    log_path = os.path.join(data_dir, "logs", "usage.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # 默认配置内容单一归宿（架构评审 R4 收尾）：曾自持一份拷贝，改默认
    # 只改一边会静默漂移
    from services.suanpan_runtime import default_config_yaml
    default_yaml = default_config_yaml(usage_log_path=log_path)
    from shared import config_store
    ok = config_store.atomic_write(sp_path, default_yaml, mode=0o600)
    if not ok:
        raise OSError(f"无法创建默认配置 {sp_path}")
    return True


def redirect_paths(sp_path: str, mp_path: str, claude_settings_path: str,
                   agent_paths: dict | None = None) -> None:
    """重定向 config_store.PATHS（文档化的单一运行时重定向点）。

    agent_paths（ADR-010 M4）：可选 {codex_config|opencode_config|
    zcode_config: path}——sync-agent 子命令用。
    """
    from shared import config_store
    updates = {
        "sp": sp_path,
        "mp": mp_path,
        "claude_settings": claude_settings_path,
    }
    updates.update(agent_paths or {})
    config_store.PATHS.update(updates)


def run_sync(sp_path: str, mp_path: str, claude_settings_path: str,
             dry_run: bool = False) -> int:
    """同步 Claude Code 配置：重定向 PATHS 后复用 claude_code_setup.

    dry_run=True 走 preview()（只出逐键 diff）；否则 setup()（幂等写入，
    首写 .bak，token 掩码）。结果以 JSON 打到 stdout；退出码 0/1。
    """
    redirect_paths(sp_path, mp_path, claude_settings_path)
    from services import claude_code_setup
    if dry_run:
        result = claude_code_setup.preview()
    else:
        result = claude_code_setup.setup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def run_sync_agent(agent: str, dry_run: bool = False,
                   options_json: str | None = None) -> int:
    """ADR-010 M4：sync-agent <name> [--dry-run] [--options JSON]——
    与 sync-claude-code 同模式（重定向 PATHS 后复用 agent_preview/
    agent_setup）。"""
    paths = default_paths()
    redirect_paths(
        paths["sp"], paths["mp"], paths["claude_settings"],
        agent_paths={
            "codex_config": paths["codex_config"],
            "opencode_config": paths["opencode_config"],
            "zcode_config": paths["zcode_config"],
        })
    from services import claude_code_setup
    options = json.loads(options_json) if options_json else None
    if dry_run:
        result = claude_code_setup.agent_preview(agent, options)
    else:
        result = claude_code_setup.agent_setup(agent, options)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def load_app(sp_path: str):
    """serve 工厂 seam：引导默认配置 → load_config → create_app.

    不在此跑 uvicorn——测试经本函数拿到 (app, listen_port) 即可验证
    /health 与端口。绕开 run_from_config_path 的回环守卫（见模块
    docstring）：绑定 0.0.0.0 由 run_serve 决定。
    """
    bootstrap_default_config(sp_path, os.path.dirname(sp_path) or ".")
    from suanpan.config import load_config
    from suanpan.main import create_app
    config = load_config(sp_path)
    return create_app(config, config_path=sp_path), config.listen_port


def _watchdog_loop(runner):
    """容器内网关健康对账：装配共享策略（services/gateway_watchdog）。

    与 macOS 版同一策略、同一默认参数（5s 审计节奏 × 3 连失配 ≈15s
    检出 + 30s 失败退避）——此前此处是丢了阈值与退避的简化手抄：
    单采样落进合法 reload 的 3-5s 端口空窗即误判僵尸态，start() 内含
    stop()，与 reload 线程竞态。主线程内联执行自愈（容器主循环无
    并发面）。entry 只装配——策略体抄写视为回归（CONTEXT.md 部署形态）。
    """
    wd = GatewayWatchdog(
        # 惰性绑定：属性解析推迟到首拍审计（与旧循环的调用时机一致，
        # serve 测试的 FakeRunner 在首个 sleep 即中断）
        audit_fn=lambda: runner.audit(),
        start_fn=runner.start,
        error_fn=lambda: runner.error,
        log=_StderrLog(),
    )
    while True:
        time.sleep(1.0)
        wd.tick()


class _StderrLog:
    """装配 adapter：策略模块的 logging 面落到容器 stderr（print，与
    entry 其余输出同一通道；未配置 handler 时 INFO 级会被 lastResort
    丢弃，不该静默）。"""

    @staticmethod
    def _emit(msg, *args):
        text = msg % args if args else msg
        print("watchdog: " + text, file=sys.stderr, flush=True)

    def info(self, msg, *args):
        self._emit(msg, *args)

    def warning(self, msg, *args):
        self._emit(msg, *args)


def run_serve() -> int:
    """serve 模式：网关绑 0.0.0.0 + 配置页面 :9528（共用同一 token）。

    SuanpanRuntime(bind_host="0.0.0.0") 跑网关；config server 的
    on_sp_saved = 运行中 reload / 已停 start——配置页保存 → 网关热重载，
    网关首启失败后 web 修复也能拉起（与 macOS 版同一运行时，仅绑定
    地址形态不同）。
    """
    paths = default_paths()
    # 先重定向再构造——SuanpanRuntime 经 PATHS["sp"] 取配置路径；
    # 缺配置文件时 bootstrap 首启自建（usage_log 指到数据卷，重建不丢），
    # runner 的 _ensure_config 兜底
    redirect_paths(paths["sp"], paths["mp"], paths["claude_settings"])
    recover_pending_txn()
    bootstrap_default_config(paths["sp"], os.path.dirname(paths["sp"]))
    runner = SuanpanRuntime(bind_host="0.0.0.0")
    if not runner.start():
        # #48 T6b：网关死亡绝不静默佯活（容器健康假象骗过编排）；不退
        # 出——配置页正是修复坏配置的路径（网关起不来的主因），保活供
        # web 修复后经 on_sp_saved 热重载拉起。
        print(f"网关启动失败：{runner.error[:200]}", file=sys.stderr)
    # 配置页面与网关同容器、同 token——config-ui 失败不阻塞网关（best-effort）
    # 保存回调 = reload-or-start 语义（单一归宿 SuanpanRuntime
    # .reload_or_start）——网关首启失败后 web 修复的闭环靠 start 补拉起
    cfg = make_config_server(paths["mp"], paths["sp"],
                             on_sp_saved=runner.reload_or_start)
    ok = cfg.start()
    if ok:
        print(f"配置页面: {cfg.url}  Bearer token: {cfg.token}", flush=True)
    else:
        print("config-ui 启动失败（9528 占用？），仅跑网关", file=sys.stderr)
    # 主线程阻塞保活 + 网关对账（watchdog：compose 无 healthcheck，
    # 进程活着但网关线程死了时容器不会重启——进程内自愈补上这个缺口；
    # 策略与 macOS 版共享单一归宿，阈值吸收合法 reload 空窗）
    try:
        _watchdog_loop(runner)
    except KeyboardInterrupt:
        runner.stop()
        cfg.stop()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] == "serve":
        if len(argv) > 1:
            print(f"serve 不接受参数: {' '.join(argv[1:])}", file=sys.stderr)
            return 2
        return run_serve()
    if argv[0] == "sync-claude-code":
        rest = argv[1:]
        unknown = [a for a in rest if a != "--dry-run"]
        if unknown:
            print(f"未知参数: {' '.join(unknown)}", file=sys.stderr)
            return 2
        paths = default_paths()
        return run_sync(paths["sp"], paths["mp"], paths["claude_settings"],
                        dry_run="--dry-run" in rest)
    if argv[0] == "sync-agent":
        rest = argv[1:]
        if not rest:
            print("用法: sync-agent <codex|opencode|zcode> "
                  "[--dry-run] [--options JSON]", file=sys.stderr)
            return 2
        agent = rest[0]
        options_json = None
        dry_run = False
        i = 1
        while i < len(rest):
            if rest[i] == "--dry-run":
                dry_run = True
            elif rest[i] == "--options" and i + 1 < len(rest):
                options_json = rest[i + 1]
                i += 1
            else:
                print(f"未知参数: {rest[i]}", file=sys.stderr)
                return 2
            i += 1
        return run_sync_agent(agent, dry_run=dry_run,
                              options_json=options_json)
    if argv[0] == "config-ui":
        rest = argv[1:]
        if rest:
            print(f"config-ui 不接受参数: {' '.join(rest)}", file=sys.stderr)
            return 2
        return run_config_ui()
    if argv[0] == "config-token":
        paths = default_paths()
        # R9：与其余子命令共用接缝（不调 redirect_paths 时 journal 落
        # ~/.suanpan.yaml.txn.json 而非 /data 卷——非 root 即写失败）
        redirect_paths(paths["sp"], paths["mp"], paths["claude_settings"])
        print(config_token(paths["mp"]))
        return 0
    print("用法: entry.py [serve | sync-claude-code [--dry-run] | "
          "sync-agent <name> [--dry-run] [--options JSON] | config-ui | "
          "config-token]",
          file=sys.stderr)
    return 2


# ── config-ui：Web 配置页面（:9528）──────────────────────────────────
# 复用参数化的 services.config_server.ConfigServer（纯 stdlib）：
# Docker 差异全部是 make_config_server 的构造参数（0.0.0.0 + 卷内固定
# token）。token 复用 #22 的 local_client_token（get_local_token 幂等，
# 零新 secret）。


def config_token(mp_path: str) -> str:
    """config-ui 的 bearer token = 本地客户端 token（幂等读取）。"""
    from mpconf.local_token import get_local_token
    return get_local_token(mp_path)


from services.config_server import CONFIG_PORT


def make_config_server(mp_path: str, sp_path: str, on_sp_saved=None,
                       port: int = CONFIG_PORT):
    """Docker 形态的 config server 装配：差异全部是构造参数。

    重定向 PATHS 三键后构造参数化 ConfigServer——绑 0.0.0.0（容器外
    经宿主机端口映射可达）、token 取配置卷的 local_client_token
    （与 sync 同源，零新 secret）。私有符号接触为零。
    """
    paths = default_paths()
    redirect_paths(sp_path, mp_path, paths["claude_settings"])
    from services.config_server import ConfigServer
    return ConfigServer(port=port, bind_host="0.0.0.0",
                        token=config_token(mp_path),
                        on_sp_saved=on_sp_saved)


def run_config_ui() -> int:
    """config-ui 模式：启动 :9528 配置页面（容器内阻塞运行）。"""
    paths = default_paths()
    redirect_paths(paths["sp"], paths["mp"], paths["claude_settings"])
    recover_pending_txn()
    bootstrap_default_config(paths["sp"], os.path.dirname(paths["sp"]))
    srv = make_config_server(paths["mp"], paths["sp"])
    if not srv.start():
        print("config server 启动失败（9528 端口占用？）", file=sys.stderr)
        return 1
    print(f"配置页面: {srv.url}", flush=True)
    print(f"Bearer token（浏览器带 Authorization 头访问）: {srv.token}",
          flush=True)
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
