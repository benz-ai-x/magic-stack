"""Architecture import guard — 分层 DAG 只向下（P4 守卫，随 P1/P2 落地）。

层次表（数字越小越底层；同域内互访自由，跨层只许向下 import）：

    0  叶子层   shared/ util.py        —— 跨域纯逻辑/工具，不识任何域
    1  域层     tunnel/ mpconf/ capture/ sysctl/ suanpan/
               mount/ vpn/
    2  编排/UI  services/ shellui/
    3  装配层   app.py docker/

域间横向 import（mpconf→capture、sysctl→capture、suanpan→mpconf、
tunnel→services…）曾是真实发生过的漂移——本守卫把它们钉死在测试里：
新增横边会在这里红，而不是在三个月后的依赖迷宫里红。

tests/ tools/ scripts/ 不在守卫范围（测试与开发工具可自由引用）。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 顶层名 → 层号。不在表里的 import（stdlib/第三方）不管。
_LAYERS = {
    "shared": 0, "util": 0,
    "tunnel": 1, "mpconf": 1, "capture": 1, "sysctl": 1, "suanpan": 1,
    "mount": 1, "vpn": 1,
    "services": 2, "shellui": 2,
    "app": 3, "docker": 3,
}

# 根平铺文件与包同名映射：app.py → app(3)，util.py → util(0)
_ROOT_FILES = {"app.py": "app", "util.py": "util"}

# 守卫覆盖的产品代码根（tests/tools/scripts 除外）
_PACKAGES = ("shared", "tunnel", "mpconf", "shellui", "capture",
             "sysctl", "services", "suanpan", "docker", "mount", "vpn")

# 设计内同层耦合白名单——每条必须带理由；新增横边不在此列即红。
_ALLOWED_SAME_LAYER = {
    # config_state 是 mp+sp 双文件的唯一事务边界：prepare/commit 需要
    # suanpan 的 pydantic schema 与路由文法做全量校验（CONTEXT.md 配置事务）
    ("mpconf", "suanpan"),
    # ADR-007：ssh 调用策略（argv 构建/host-key 三件套/认证注入/失败
    # 分类）单一归宿在 tunnel/ssh_launch——mount 域的远程安装与会话
    # 必须复用同一份策略，不得自建 ssh argv
    ("mount", "tunnel"),
}


def _iter_product_files():
    for name, layer_key in _ROOT_FILES.items():
        yield ROOT / name, _LAYERS[layer_key]
    for pkg in _PACKAGES:
        base = ROOT / pkg
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p, _LAYERS[pkg]


def _imported_roots(tree):
    """AST 里出现的本仓顶层 import 目标（含函数内延迟 import）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


class TestLayeredImportDag:
    def test_no_upward_or_cross_domain_imports(self):
        """两条规则：只许向下 import；同层只许同域（跨域=横向，禁止）。"""
        violations = []
        for path, own_layer in _iter_product_files():
            own_domain = (path.parent.name if path.parent != ROOT
                          else path.stem)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target in _imported_roots(tree):
                if target not in _LAYERS or target == own_domain:
                    continue
                target_layer = _LAYERS[target]
                if (own_domain, target) in _ALLOWED_SAME_LAYER:
                    continue
                if target_layer > own_layer:
                    why = f"imports upward: {target} (L{target_layer})"
                elif target_layer == own_layer:
                    why = f"cross-domain at same layer: {target} (L{target_layer})"
                else:
                    continue
                violations.append(
                    f"{path.relative_to(ROOT)} (L{own_layer}) {why}")
        assert not violations, (
            "分层 DAG 违例——只许向下、同层只许同域：\n  "
            + "\n  ".join(violations))

    def test_leaf_layer_knows_no_domain(self):
        """叶子层（shared/ 与 util.py）不得 import 任何域/编排/装配层。"""
        leaves = [ROOT / "util.py"] + sorted((ROOT / "shared").glob("*.py"))
        for path in leaves:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target in _imported_roots(tree):
                if target in _LAYERS and target not in ("shared", "util"):
                    raise AssertionError(
                        f"叶子层 {path.relative_to(ROOT)} 依赖了 {target} "
                        "——叶子必须零域知识")
