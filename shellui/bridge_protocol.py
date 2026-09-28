"""设置窗桥接协议核心 — pure-Python half of the WKWebView bridge.

Owns the message protocol between config_ui.html (JS) and the native
settings window: message dispatch, the dirty-state machine backing the
window close-guard, and outbound JS construction. No PyObjC/AppKit
imports, so the whole protocol is testable in plain pytest —
webview_window.py is only a thin ObjC adapter over BridgeCore.

Protocol v1 (single "bridge" script-message channel, {type, payload} JSON):
  JS → PY  {type:"dirtyState",     payload:{dirty: bool}}
           {type:"pickKeyFile",    payload:{field: "sshKey"}}
           {type:"reconnectProxy", payload:{}}            — 无条件重连代理（用户显式点击）
           {type:"reconnectProxy", payload:{tunnel_id: id}} — 显式重连服务器：
             原生侧按最新已保存代理角色选择接入或转发会话
           {type:"reconnectProxy", payload:{if_connected: true}} — 守卫重连：仅当
             隧道当前已连接才执行（保存端口转发后的自动应用；未连接绝不拉起）
           {type:"reconnectProxy", payload:{if_connected: true, tunnel_id: id}}
             — 定向守卫重连：tunnel_id 指定转发会话时按该会话自身的
             连接态守卫（多活；不填 tunnel_id = 代理会话）
           {type:"forwardSession", payload:{tunnel_id: id, action: "start"|"stop"}}
             — 转发会话启停（设置窗 detail-bar 按钮；多活 v0.9）
           {type:"nfsMountToggle", payload:{tunnel_id: id, name: str, action: "mount"|"unmount"}}
             — NFS 挂载启停（ADR-007；会话/挂载编排归 MountCoordinator，
               JS 无运行时状态）
           {type:"openPath",       payload:{kind: "captureDir"}}
  PY → JS  {type:"keyFilePicked", payload:{field, path}}
           delivered via window.__native.receive(<json>)

Design rules that keep the historical crash classes from recurring:
- json.dumps is the ONLY escaping layer; Python never interpolates JS source
  and never names a JS DOM selector (the JS side owns its DOM).
- handle_message never raises — the ObjC delegate must stay exception-free.
- JS owns dirty truth; Python mirrors it through typed messages only.
"""
import json
import logging

logger = logging.getLogger("magic-proxy.bridge")

# Fields the native side knows how to service with a file picker.
PICKABLE_FIELDS = frozenset({"sshKey"})

# Path kinds the native side may reveal (closed set, like PICKABLE_FIELDS —
# the JS side never names an arbitrary filesystem path to open).
OPENABLE_KINDS = frozenset({"captureDir"})

ACTION_SHOW_OPEN_PANEL = "showOpenPanel"
ACTION_VPN_OPEN_PANEL = "vpnOpenPanel"
ACTION_RECONNECT_PROXY = "reconnectProxy"
ACTION_STOP_PROXY = "stopProxy"
ACTION_OPEN_PATH = "openPath"
ACTION_COPY_AGENT_INSTRUCTIONS = "copyAgentInstructions"
ACTION_FORWARD_SESSION = "forwardSession"
ACTION_NFS_MOUNT_TOGGLE = "nfsMountToggle"

# forwardSession 的合法动作闭集（action 字段）
FORWARD_SESSION_ACTIONS = frozenset({"start", "stop"})

# nfsMountToggle 的合法动作闭集（action 字段）
NFS_MOUNT_ACTIONS = frozenset({"mount", "unmount"})


def _plain(obj):
    """Recursively normalize ObjC-bridged containers (NSDictionary/NSArray)
    into plain Python dict/list. Anything unconvertible becomes {} / passes
    through unchanged — normalization never raises."""
    if isinstance(obj, dict):
        try:
            return {k: _plain(v) for k, v in obj.items()}
        except Exception:
            return {}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    keys = getattr(obj, "keys", None)
    if callable(keys):
        try:
            return {k: _plain(obj[k]) for k in obj.keys()}
        except Exception:
            return {}
    return obj


class BridgeCore:
    """Dict-in/dict-out bridge protocol. Never raises to the ObjC caller."""

    def __init__(self):
        self._dirty = False

    @property
    def dirty(self):
        """Close-guard state mirrored from the JS side."""
        return self._dirty

    def handle_message(self, msg):
        """Dispatch one bridge message (plain or ObjC-bridged); return actions."""
        try:
            return self._dispatch(_plain(msg))
        except Exception:
            logger.exception("bridge message dropped: %r", msg)
            return []

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            logger.warning("bridge message not a dict: %r", msg)
            return []
        mtype = msg.get("type")
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if mtype == "dirtyState":
            self._dirty = bool(payload.get("dirty"))
            return []
        if mtype == "pickKeyFile":
            field = payload.get("field")
            if field in PICKABLE_FIELDS:
                return [{"type": ACTION_SHOW_OPEN_PANEL, "field": field}]
            logger.warning("pickKeyFile for unknown field: %r", field)
            return []
        if mtype == "pickVpnProfile":
            return [{"type": ACTION_VPN_OPEN_PANEL}]
        if mtype == "stopProxy":
            # 关闭代理（设置窗按钮）：停全部 SSH 会话 + 卸载 NFS——与
            # VPN 拆除屏障同一 teardown 半边；执行纪律归 app/intents
            return [{"type": ACTION_STOP_PROXY}]
        if mtype == "reconnectProxy":
            # Equivalent of the menu-bar 重新连接 item; the app-level handler
            # owns threading and the actual connection orchestration.
            # if_connected: 守卫变体——保存端口转发后的自动应用，未连接
            # 的隧道绝不因此被拉起（显式点击路径不带此旗标）。
            # tunnel_id: 显式点击按已保存角色分派；守卫变体只定向转发。
            # 缺省 id = 代理接入。
            action = {"type": ACTION_RECONNECT_PROXY,
                      "if_connected": bool(payload.get("if_connected"))}
            tid = payload.get("tunnel_id")
            if isinstance(tid, str) and tid:
                action["tunnel_id"] = tid
            return [action]
        if mtype == "forwardSession":
            # 多活：设置窗「启动/停止端口转发」——转发会话启停经原生侧
            # （会话编排归 coordinator，JS 无运行时状态）
            tid = payload.get("tunnel_id")
            act = payload.get("action")
            if isinstance(tid, str) and tid and act in FORWARD_SESSION_ACTIONS:
                return [{"type": ACTION_FORWARD_SESSION,
                         "tunnel_id": tid, "action": act}]
            logger.warning("forwardSession bad payload: %r", payload)
            return []
        if mtype == "nfsMountToggle":
            # ADR-007：设置窗「挂载/卸载」——MountCoordinator 编排，
            # JS 无运行时状态（mount_states 由 /api/state 装饰回读）
            tid = payload.get("tunnel_id")
            name = payload.get("name")
            act = payload.get("action")
            if (isinstance(tid, str) and tid and isinstance(name, str)
                    and name and act in NFS_MOUNT_ACTIONS):
                return [{"type": ACTION_NFS_MOUNT_TOGGLE,
                         "tunnel_id": tid, "name": name, "action": act}]
            logger.warning("nfsMountToggle bad payload: %r", payload)
            return []
        if mtype == "openPath":
            kind = payload.get("kind")
            if kind in OPENABLE_KINDS:
                return [{"type": ACTION_OPEN_PATH, "kind": kind}]
            logger.warning("openPath for unknown kind: %r", kind)
            return []
        if mtype == "copyAgentInstructions":
            # #70 S13：指令文本含 Bearer token——拼装必须在原生侧（JS 永不
            # 接触凭证）；app 层持 expected_token 直接把完整文本上剪贴板
            return [{"type": ACTION_COPY_AGENT_INSTRUCTIONS}]
        logger.warning("unknown bridge message type: %r", mtype)
        return []

    @staticmethod
    def build_fill_js(field, path):
        """Build JS delivering a picked path to the JS-owned receiver."""
        msg = {"type": "keyFilePicked", "payload": {"field": field, "path": path}}
        return "window.__native&&window.__native.receive(%s)" % json.dumps(
            msg, ensure_ascii=False)
