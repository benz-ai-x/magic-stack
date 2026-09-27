"""mp 配置的分域校验器（架构评审候选 2：prepare 收缩为 orchestrator）。

输入是**未经 merge 的保存候选**（UI/agent 直接 PUT 上来的形状）；输出
中文错误行——文案与检查顺序被 test_config_state / test_config_nfs 经
prepare 钉死，本模块的存在让新域校验与该域的归一/解析知识同文件域演
进（locality）。任何校验都不触碰磁盘。

与运行时共用的规则单一归宿：挂载点默认目录解析在
mpconf.config.resolve_mount_dir（校验与运行时同一语义，不复制规则）。
"""
from shared.defaults import PORT_MAX as _PORT_MAX

_MP_PORTS = ("socks5_port", "http_listen_port", "capture_port", "config_port")
_RETENTION_MAX = 3650        # 十年封顶：再大属单位填错


def numeric_errors(mp) -> list:
    """顶层数值字段：mp 四端口 + retention_days（None/空串 = 未填跳过）。"""
    errors = []
    for field in _MP_PORTS:
        port = mp.get(field)
        if port in (None, ""):
            continue
        if not isinstance(port, int) or not 1 <= port <= _PORT_MAX:
            errors.append(f"{field} 端口无效（须 1..65535）")
    retention = mp.get("retention_days")
    if retention is not None and (
            not isinstance(retention, int)
            or not 0 <= retention <= _RETENTION_MAX):
        errors.append(f"retention_days 无效（须 0..{_RETENTION_MAX}）")
    return errors



def _ssh_svc(server) -> dict:
    """v2 形状：SSH 隧道服务节点（services.ssh；非 dict 形状安全返回 {}）。"""
    svc = server.get("services") if isinstance(server.get("services"), dict) else {}
    ssh = svc.get("ssh")
    return ssh if isinstance(ssh, dict) else {}


def _nfs_svc(server):
    """v2 形状：NFS 服务节点（services.nfs；非 dict 返回 None 保持
    「未配置不校验」语义）。"""
    svc = server.get("services") if isinstance(server.get("services"), dict) else {}
    nfs = svc.get("nfs")
    return nfs if isinstance(nfs, dict) else None

def server_rows_errors(mp) -> list:
    """逐服务器行校验：SSH 隧道服务的转发实例 + NFS 服务的挂载实例。

    merge 前：保存候选必须规整——字符串端口的读时兼容只发生在 load
    路径的 normalize_forwards。NFS 名字/路径/端口是运行时标识与安全
    边界（远程路径进 root 脚本、本地目录进 mount_nfs argv），形状不
    对必须在落盘前拦下。
    """
    errors = []
    for _ti, _t in enumerate(mp.get("servers") or []):
        if not isinstance(_t, dict):
            continue
        _tname = _t.get("name") or f"#{_ti}"
        _ssh = _t.get("ssh")
        if _ssh is not None and not isinstance(_ssh, dict):
            errors.append(f"服务器 {_tname} 的 ssh 必须是对象")
        _forwards = _ssh_svc(_t).get("forwards")
        if _forwards is None:
            pass
        elif not isinstance(_forwards, list):
            errors.append(f"服务器 {_tname} 的 forwards 必须是列表")
        else:
            errors += _forward_rows(_tname, _forwards)
        _nfs = _nfs_svc(_t)
        if _nfs is not None:
            errors += _nfs_errors(_tname, _nfs)
        _vpn = _vpn_svc(_t)
        if _vpn is not None:
            errors += _vpn_errors(_tname, _vpn)
    return errors


def _vpn_svc(server):
    """v2 形状：OpenVPN 服务节点（services.openvpn；非 dict 返回 None）。"""
    svc = server.get("services") if isinstance(server.get("services"), dict) else {}
    vpn = svc.get("openvpn")
    return vpn if isinstance(vpn, dict) else None


def _vpn_errors(tname, vpn) -> list:
    """openvpn 服务节校验（M2 最小面）：auth 枚举 + userpass 须用户名。
    profile 正文不在 config（独立 0600 文件，spec §4）——此处只校配置面；
    与设置窗 validateConfig 的 openvpn 段是镜像关系（校验镜像纪律）。"""
    errors = []
    _auth = vpn.get("auth")
    if _auth not in (None, "", "none", "userpass"):
        errors.append(f"服务器 {tname} 的 openvpn.auth 无效（须 none 或 userpass）")
    if _auth == "userpass":
        _user = vpn.get("username")
        if not isinstance(_user, str) or not _user.strip():
            errors.append(f"服务器 {tname} 启用 OpenVPN 密码认证时用户名不能为空")
    return errors


def _forward_rows(tname, forwards) -> list:
    errors = []
    for _fi, _f in enumerate(forwards):
        if not isinstance(_f, dict):
            errors.append(
                f"服务器 {tname} 的第 {_fi + 1} 条端口转发必须是对象")
            continue
        for _key in ("local_port", "remote_port"):
            _v = _f.get(_key)
            if (not isinstance(_v, int) or isinstance(_v, bool)
                    or not 1 <= _v <= _PORT_MAX):
                errors.append(
                    f"服务器 {tname} 第 {_fi + 1} 条转发的 "
                    f"{_key} 无效（须 1..65535）")
        _rh = _f.get("remote_host")
        if _rh is None:
            _rh = "127.0.0.1"
        if (not isinstance(_rh, str) or not _rh.strip()
                or any(c.isspace() for c in _rh) or ":" in _rh):
            errors.append(
                f"服务器 {tname} 第 {_fi + 1} 条转发的 remote_host "
                "无效（须主机名或 IPv4 地址，暂不支持 IPv6）")
    return errors


def _nfs_errors(tname, nfs) -> list:
    errors = []
    _nport = nfs.get("local_port")
    if _nport not in (None, "") and (
            not isinstance(_nport, int)
            or isinstance(_nport, bool)
            or not 1 <= _nport <= _PORT_MAX):
        errors.append(
            f"服务器 {tname} 的 NFS 本地端口无效（须 1..65535）")
    _mounts = nfs.get("mounts")
    if _mounts is not None and not isinstance(_mounts, list):
        errors.append(f"服务器 {tname} 的 nfs.mounts 必须是列表")
        _mounts = []
    _names = set()
    for _mi, _m in enumerate(_mounts or []):
        if not isinstance(_m, dict):
            errors.append(
                f"服务器 {tname} 的第 {_mi + 1} 条 NFS 挂载必须是对象")
            continue
        _label = _m.get("name") or f"#{_mi + 1}"
        _mname = _m.get("name")
        if not isinstance(_mname, str) or not _mname.strip():
            errors.append(
                f"服务器 {tname} 的第 {_mi + 1} 条 NFS 挂载名不能为空")
        elif _mname.strip() in _names:
            errors.append(
                f"服务器 {tname} 的 NFS 挂载名 {_mname.strip()} 重复")
        else:
            _names.add(_mname.strip())
        _rp = _m.get("remote_path")
        if (not isinstance(_rp, str)
                or not _rp.strip().startswith("/")):
            errors.append(
                f"NFS 挂载 {_label} 的远程路径必须是绝对路径（以 / 开头）")
        _ld = _m.get("local_dir")
        if (_ld not in (None, "")
                and (not isinstance(_ld, str)
                     or not _ld.strip().startswith("/"))):
            errors.append(
                f"NFS 挂载 {_label} 的本地目录必须是绝对路径（以 / 开头）")
    return errors


def port_conflict_errors(mp, sp) -> list:
    """全局端口冲突扫描（#70 S10 上收事务边界：JS validateConfig 只在
    浏览器层——agent 直接 curl /api/state 可绕过；prepare 兜底拦同值
    冲突，落盘后才由 bind 失败就太迟）。

    mp/sp 任一可为 None（该侧不参与）。多活（v0.9）：转发本地端口全
    局唯一——任意服务器可并行运行，两台服务器抢同端口会让双方在
    ExitOnForwardFailure 下互顶死循环（v0.8 的「跨隧道合法」以单活为
    前提，随多活作废）。NFS 隧道与转发会话并行——本地端口同一命名空
    间；只查「实际在用」的 nfs（enabled 或配置了挂载）：merge 会给每
    台服务器填默认 nfs 节（enabled=False、无挂载、12049），纯默认节点
    不占端口，不得让两台服务器互报假冲突。
    """
    errors = []
    port_refs = []
    if mp is not None:
        for _f in _MP_PORTS:
            _v = mp.get(_f)
            if isinstance(_v, int) and not isinstance(_v, bool):
                port_refs.append((_f, _v))
        for _ti, _t in enumerate(mp.get("servers") or []):
            if not isinstance(_t, dict):
                continue
            _tname = _t.get("name") or f"#{_ti}"
            _fw_seen = set()
            for _f in _ssh_svc(_t).get("forwards") or []:
                if not isinstance(_f, dict):
                    continue
                _lp = _f.get("local_port")
                if not isinstance(_lp, int) or isinstance(_lp, bool):
                    continue
                # 停用行不进会话 -L 集合、不占本地端口——退出冲突检查
                # （同隧道重复检查同步退出，否则停 A 配 B 同端口被误拦；
                # NFS "只在用才占端口" 同款口径）。行级形状校验不受
                # 停用影响（tunnel_rows_errors 全量）。
                if _f.get("enabled") is False:
                    continue
                if _lp in _fw_seen:
                    errors.append(
                        f"服务器 {_tname} 的转发本地端口 {_lp} 重复")
                else:
                    _fw_seen.add(_lp)
                    port_refs.append(
                        (f"服务器 {_tname} 端口转发本地端口", _lp))
            _nfs = _nfs_svc(_t)
            if isinstance(_nfs, dict) and (
                    _nfs.get("enabled") is True or _nfs.get("mounts")):
                _np = _nfs.get("local_port")
                if (isinstance(_np, int) and not isinstance(_np, bool)
                        and 1 <= _np <= _PORT_MAX):
                    port_refs.append(
                        (f"服务器 {_tname} NFS 本地端口", _np))
    if sp is not None:
        _v = sp.get("listen_port")
        if isinstance(_v, int) and not isinstance(_v, bool):
            port_refs.append(("listen_port", _v))
    _seen = {}
    for _name, _p in port_refs:
        if _p in _seen:
            errors.append(
                f"端口冲突：{_seen[_p]} 与 {_name} 同为 {_p}")
        else:
            _seen[_p] = _name
    return errors


def mount_dir_conflict_errors(mp) -> list:
    """NFS 挂载点全局唯一（解析默认值与运行时同一归宿
    mpconf.config.resolve_mount_dir）：两个挂载抢同一目录，后挂的会顶
    掉先挂的。"""
    if mp is None:
        return []
    from mpconf.config import resolve_mount_dir as _resolve_dir
    errors = []
    _dirs_seen = {}
    for _ti, _t in enumerate(mp.get("servers") or []):
        if not isinstance(_t, dict):
            continue
        _tname = _t.get("name") or f"#{_ti}"
        _nfs = _nfs_svc(_t)
        _nfs_mounts = (_nfs.get("mounts")
                       if isinstance(_nfs, dict) else None) or []
        for _m in _nfs_mounts:
            if not isinstance(_m, dict):
                continue
            _label = str(_m.get("name") or "?").strip()
            if not _label:
                continue  # 空名已在 tunnel_rows_errors 拦下
            _dir = _resolve_dir(_m)
            _who = f"服务器 {_tname} 的挂载 {_label}"
            if _dir in _dirs_seen:
                errors.append(
                    f"挂载点冲突：{_dirs_seen[_dir]} 与 {_who} 同为 {_dir}")
            else:
                _dirs_seen[_dir] = _who
    return errors
