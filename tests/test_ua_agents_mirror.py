"""UA 表 × Agent 注册表 mirror 测试（R8-C5 半刀）。

suanpan/proxy._UA_AGENTS 的 needle→agent 映射与 claude_code_setup 的
Agent 身份面（registry ids + CC）此前零对账——漏登条目的后果是用量
统计静默归「未识别」桶。照 capture host mirror（tests/
test_capture_host_mirror.py）的范本钉双向：
- 每个 UA 目标 agent 必须是已知 Agent 身份（registry ∪ CC）；
- 每个 Agent 身份必须有 ≥1 个 UA needle（漏登即红）。
"""
import unittest

from suanpan.proxy import _UA_AGENTS
from services import claude_code_setup


def _known_agent_ids() -> set:
    ids = set(claude_code_setup._AGENT_REGISTRY)
    ids.add("claude-code")   # agents_status 的首位成员（registry 外硬编码）
    return ids


class TestUaAgentsMirror(unittest.TestCase):
    def test_ua_targets_are_known_agents(self):
        known = _known_agent_ids()
        for needle, agent in _UA_AGENTS:
            self.assertIn(
                agent, known,
                f"UA needle {needle!r} 指向未知 agent {agent!r}——"
                f"用量将静默落「未识别」桶（新 Agent 须同步 _UA_AGENTS）")

    def test_every_agent_has_a_ua_needle(self):
        needles = {agent for _needle, agent in _UA_AGENTS}
        for agent in sorted(_known_agent_ids()):
            self.assertIn(
                agent, needles,
                f"Agent {agent!r} 无 UA needle——其用量将无法归因"
                f"（新增 Agent 必须在 _UA_AGENTS 挂 needle）")


if __name__ == "__main__":
    unittest.main()
