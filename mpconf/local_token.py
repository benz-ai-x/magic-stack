"""本地客户端 token（issue #9，决策 A×4）：每安装实例专用随机 token.

随机生成一次、存 `~/.magic-proxy.json` 的 `local_client_token` 字段；
单活轮换——任意时刻一个有效值。Claude Code 同步写入该 token；网关只用
它做本地客户端认证，并在任何 Provider 出站前无条件剥除。明文永不回显
于 UI/日志/diff（掩码布尔契约）。

R8-C3：写径收进 ConfigStateStore.update_mp（「唯一事务边界」承诺兑现）
——此前自持 read-modify-write 有三重隐患：① 损坏主文件被折叠 {} 后整
文件覆写成单键（配置蒸发；Docker 路径无 .bak 前备即全损）；② journal
崩溃重放用提交前候选覆盖 token 写入（已分发的 ANTHROPIC_AUTH_TOKEN
静默 401）；③ 与 UI 保存流无共享锁序的 lost-update 竞窗。update_mp 的
损坏拒写分支恰好堵住 ①；journal 载荷携带完整候选（含 token）闭合 ②；
同一事务管线消 ③。
"""
from __future__ import annotations

import json
import logging
import secrets

FIELD = "local_client_token"

logger = logging.getLogger("magic-proxy.local-token")


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_local_token(path: str) -> str:
    """幂等读取；不存在则生成一次并经事务边界落盘（0600）。

    写失败（含主文件损坏拒写）时 token 仍返回——本次会话可用，下次
    启动重新生成；**绝不以覆写换持久化**。
    """
    tok = _read(path).get(FIELD)
    if isinstance(tok, str) and tok:
        return tok
    tok = secrets.token_hex(16)
    from mpconf.config_state import ConfigStateStore
    from shared.identity import IdentityMigrationError
    try:
        # 字段级 upsert：update_mp 按事务触面自动跳过行校验（R9-C3——
        # 存量空 host 行曾把 token 铸造连坐成「每次调用轮换」，分发
        # 的 token 静默 401）
        result = ConfigStateStore(mp_path=path, keychain=None).update_mp(
            lambda c: {**c, FIELD: tok})
    except IdentityMigrationError as e:
        # 重复 id 的迁移异常（load_config 上抛）：macOS 侧由
        # claude_code_setup 的 except ValueError 兜住，Docker 三入口
        # 裸奔即 boot 崩——此处按「未持久化」降级（token 仍返回）
        logger.warning("local token 未持久化（迁移异常 %s）", e)
        return tok
    if not result.ok:
        logger.warning("local token 未持久化（%s）——本次会话仍可用，"
                       "下次启动将重新生成", result.errors[:1] or result.code)
    return tok
