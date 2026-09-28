"""VpnCoordinator —— VPN 接入协调器（与 ConnectionCoordinator 对称的
接入序列单一归宿，R7-C1）。

M2 接线时连接序列住进了 app.py（~200 行域逻辑 + 错误码表 + 互斥闸 +
客户端重建 + 启动收养），与 SSH 侧「序列归协调器、app 只发意图」的
结构不对称——R6/R7 两轮盲走在同一处收敛。本模块收编：

- 连接序列：resolve 二进制 → profile 装载 → sudoers/资产重装判据 →
  runtime_conf → install → 接入层切换（注入回调）→ VpnClient 重建/起
- 安装知识单一归宿（``install_for``——设置窗「安装到系统」按钮与连接
  序列内部同款调用，不再两份手拼）
- established 服务层重建回调（转发 + NFS 会话；**绝不拉 -D**：接入
  互斥，与唤醒触发的根本差异）
- 错误码 → i18n 键两张表（域内零文案纪律：表存键名，取词经 i18n.t）
- 启动收养（残留 root openvpn 清场）
- snapshot / 菜单四字段投影（app 不再 getattr 捅 VpnClient 内部）

依赖全注入（vpn 域不横向 import tunnel/services——分层 DAG 只向下）：
conn 的 stop_access、系统代理/防睡眠收敛、服务层重建、通知、dirty 各
经回调。线程纪律仍归 services/intents（connect/disconnect 是慢操作，
后台跑）。
"""
from __future__ import annotations

import logging
import threading
from typing import NamedTuple

from shared import keychain
from shared.defaults import VPN_MANAGEMENT_PORT
from shared import i18n
from shared.server_shape import server_openvpn, servers
from vpn import dns_scripts, privilege, profile_store
from vpn.openvpn_client import (
    VpnClient, adopt_stale_openvpn, is_active_status, is_in_flight_status,
)

logger = logging.getLogger("magic-proxy.vpn-coordinator")

# VPN 结构化错误码 → 文案键（字面键名表——取词守卫纪律：表存键名，
# t() 调用点查表；键全集在 shared/locales 双侧登记）
_VPN_ERR_KEYS = {
    "auth_failed": "vpn.err.auth_failed",
    "auth_required": "vpn.err.auth_required",
    "auth_challenge_unsupported": "vpn.err.challenge",
    "cipher_mismatch": "vpn.err.cipher",
    "cert_expired": "vpn.err.cert_expired",
    "cert_invalid": "vpn.err.cert_invalid",
    "tls_error": "vpn.err.tls",
    "unreachable": "vpn.err.unreachable",
    "server_exit": "vpn.err.server_exit",
    "crashed": "vpn.err.crashed",
    "mgmt_lost": "vpn.err.mgmt_lost",
    "mgmt_attach_failed": "vpn.err.mgmt_attach",
    "start_failed": "vpn.err.start_failed",
    "fatal": "vpn.err.generic",
}
_VPN_INSTALL_ERR_KEYS = {
    "cancelled": "vpn.err.install_cancelled",
    "sudoers_verify_failed": "vpn.err.install_verify",
    "openvpn_missing": "vpn.err.no_binary_code",
    "osascript_failed": "vpn.err.install_generic",
}


def error_key(kind: str) -> str:
    """运行错误码 → i18n 键（未知码兜底 generic）。"""
    return _VPN_ERR_KEYS.get(kind, "vpn.err.generic")


def install_error_key(code: str) -> str:
    """安装错误码 → i18n 键（HTTP 端点映射文案用同一张表）。"""
    return _VPN_INSTALL_ERR_KEYS.get(code, "vpn.err.install_generic")


class MenuVpn(NamedTuple):
    """MenuState 的 VPN 四字段（app 只搬运不推导）。"""
    status: str
    server_name: str
    error_kind: str
    tun_ip: str


class VpnCoordinator:
    """VPN 接入的意图动作体 + 读侧投影（全局至多一条活跃连接，spec §5.3）。"""

    def __init__(self, *, get_config, switch_access, on_established,
                 notify, mark_dirty):
        self._get_config = get_config
        self._switch_access = switch_access
        self._on_established_cb = on_established
        self._notify_cb = notify
        self._mark_dirty_cb = mark_dirty
        self._client = None
        # 连接并发闸：连点/菜单+设置窗双入口同时触发时只跑一个
        # （屏障与 VpnClient 重建都不重入）
        self._connect_lock = threading.Lock()

    # ── 意图动作体（intents 负责线程纪律）─────────────────

    def connect(self, server):
        """连接核心（daemon 后台跑）。并发闸：重入即跳过。"""
        if not self._connect_lock.acquire(blocking=False):
            logger.info("vpn connect skipped: already in flight")
            return
        try:
            self._connect_locked(server)
        finally:
            self._connect_lock.release()

    def _connect_locked(self, server):
        logger.info("vpn connect requested: server=%s", server.get("id"))
        binary = privilege.resolve_openvpn_bin()
        if not binary:
            logger.warning("vpn connect aborted: openvpn binary missing")
            self._notify_cb(i18n.t("notify.vpn.no_binary"),
                            i18n.t("notify.vpn.no_binary_body"))
            return
        profile_text = profile_store.load_profile(server.get("id") or "")
        if not profile_text.strip():
            logger.warning("vpn connect aborted: profile empty (id=%s)",
                           server.get("id"))
            self._notify_cb(i18n.t("notify.vpn.no_profile"), "")
            return
        svc = server_openvpn(server)
        mgmt_pw = keychain.ensure_vpn_mgmt_password()
        # 重装条件：sudoers 缺失，或磁盘 dns 脚本不是当前版本（脚本
        # 0755 可读可比对；conf 0600 不可读——其重装挂脚本版本：dns/
        # conf 组成变更即 bump SCRIPTS_VERSION，杜绝「conf 用到天荒地老」）
        if not privilege.check_sudoers(binary) \
                or not dns_scripts.assets_current():
            logger.info("vpn connect: sudoers/assets outdated, installing")
            ok, code = self._install(profile_text, svc, mgmt_pw)
            if not ok:
                logger.warning("vpn connect aborted: install failed (%s)",
                               code)
                self._notify_cb(
                    i18n.t("notify.vpn.install_failed"),
                    i18n.t(install_error_key(code)))
                return
        # 接入层切换（ADR-011 修订）：只停 -D 会话 + 系统代理收敛 +
        # 防睡眠重算——转发会话与 NFS 挂载是服务层，原地不动（路由翻转
        # 断掉的由 established 事件重建）
        logger.info("vpn connect: switching access layer (-D only)")
        self._switch_access()

        def _creds():
            return (svc.get("username", ""),
                    keychain.get_vpn_password(server))

        if self._client is not None:
            self._client.stop()
        self._client = VpnClient(
            full_cmd=privilege.build_full_command(binary),
            mgmt_port=VPN_MANAGEMENT_PORT,
            mgmt_password=mgmt_pw or None,
            credentials=_creds,
            on_state_change=self._on_state_change,
            on_error=self._on_error,
        )
        self._client.start()
        self._mark_dirty_cb()

    def disconnect(self):
        client = self._client
        if client is not None:
            client.stop()
        self._mark_dirty_cb()

    def shutdown(self):
        """quit 路径：blocking=False（SIGTERM 发出即返回，等待与收养交
        后台线程——主线程裸阻塞 ~15s 的 quit 挂脸病根）。"""
        client, self._client = self._client, None
        if client is not None:
            client.stop(blocking=False)

    def check_tick(self):
        """每秒健康泵（app._on_tick 转发）。"""
        if self._client is not None:
            self._client.check()

    # ── 读侧投影（菜单 / RuntimeProjection / HTTP 翻译用）──

    def is_active(self) -> bool:
        """行为面活跃（toggle 分派用）——语义档单一归宿在
        openvpn_client.is_active_status。"""
        c = self._client
        return c is not None and is_active_status(c.vpn.status)

    def is_in_flight(self) -> bool:
        """显示面进行中（黄点档，含 exiting 不含 connected）。"""
        c = self._client
        return c is not None and is_in_flight_status(c.vpn.status)

    def status(self) -> str:
        c = self._client
        return c.vpn.status if c is not None else "idle"

    def snapshot(self):
        """RuntimeProjection.vpn 的单一来源（无客户端 → None）。"""
        c = self._client
        return c.snapshot() if c is not None else None

    def projection(self):
        """RuntimeProjection.vpn 增强（R7-C2）：快照 + 语义布尔
        （active/in_flight——JS 侧状态集字面量随之消灭）+ error_key
        （已解析 i18n 键，JS tt() 取词——error_kind 英文 token 不再
        裸进 UI）。装饰链 wholesale 透传（mp["vpn_state"] = proj.vpn）。"""
        snap = self.snapshot()
        if not snap:
            return None
        kind = snap.get("error_kind") or ""
        return {**snap,
                "active": is_active_status(snap.get("status", "")),
                "in_flight": is_in_flight_status(snap.get("status", "")),
                "error_key": error_key(kind) if kind else ""}

    def configured_server(self):
        """「已配置 VPN 的服务器」单一解析：profile_set 的第一台。"""
        for s in servers(self._get_config()):
            if isinstance(s, dict) and server_openvpn(s).get("profile_set"):
                return s
        return None

    def menu_vpn(self) -> MenuVpn:
        """MenuState 的 VPN 四字段单一投影（收口原三段式 getattr 链）。"""
        c = self._client
        st = c.vpn if c is not None else None
        server = self.configured_server()
        return MenuVpn(
            status=st.status if st is not None else "idle",
            server_name=(server or {}).get("name") or "",
            error_kind=st.error_kind if st is not None else "",
            tun_ip=st.tun_ip if st is not None else "",
        )

    # ── 同步前置 / 安装（HTTP 入口与连接序列共用）──────────

    def connect_precheck(self, server):
        """连接前置（同步可拒半边）：None=可连；"no_profile"=未导入。

        profile 判定以落盘文件为准（导入端点即时写文件）——不依赖保存
        流的 profile_set 布尔：真机案例（2026-09-27）导入后未保存，连接
        被 no_profile 弹回而用户只见「没反应」。"""
        if not (server_openvpn(server).get("profile_set")
                or profile_store.profile_exists(server.get("id") or "")):
            return "no_profile"
        return None

    def _install(self, profile_text, svc, mgmt_pw):
        """安装动作单一归宿（连接序列与 install_for 共用）。"""
        return privilege.install(
            conf_text=privilege.runtime_conf(
                profile_text, pull_dns=svc.get("pull_dns", True)),
            mgmt_password=mgmt_pw)

    def install_for(self, server):
        """安装到系统（设置窗「安装到系统」按钮；幂等）。pull_dns 读已
        保存配置——先保存再安装。返回 (ok, code)；code="no_profile" 或
        安装错误码。"""
        text = profile_store.load_profile(server.get("id") or "")
        if not text.strip():
            return False, "no_profile"
        ok, code = self._install(text, server_openvpn(server),
                                 keychain.ensure_vpn_mgmt_password())
        return ok, code

    # ── 生命周期杂项 ────────────────────────────────────────

    def startup_reconcile(self):
        """启动期收养（spec §5.4 落地到生命周期）：残留 root openvpn
        （上次崩溃/强杀后永久占管理口者）经管理口 SIGTERM + DNS 标志
        补跑。任何失败只记日志，绝不阻断启动。"""
        try:
            pw = keychain.get_vpn_mgmt_password()
            binp = privilege.resolve_openvpn_bin()
            if pw and binp and privilege.check_sudoers(binp):
                adopt_stale_openvpn(pw, VPN_MANAGEMENT_PORT, timeout=1.0)
                dns_scripts.run_reconcile()
        except Exception:
            logger.exception("vpn startup reconcile failed")

    # ── VpnClient 回调（dirty / established / 错误通知）────

    def _on_state_change(self, snap):
        """VPN 状态回调：dirty 刷菜单；established 时路由翻转已断存量
        TCP——服务层僵尸重建（注入回调；**绝不拉起 -D**：接入互斥，
        这是与唤醒触发的根本差异）。"""
        self._mark_dirty_cb()
        if isinstance(snap, dict) and snap.get("status") == "connected":
            logger.info("vpn established: rebuilding service sessions")
            self._on_established_cb()

    def _on_error(self, kind, text):
        self._notify_cb(i18n.t(error_key(kind)), text or "")
