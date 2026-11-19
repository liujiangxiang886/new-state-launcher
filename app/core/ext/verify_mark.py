"""验证标志：一次「我跑过检查」的记录，不是兼容性保证。

设计边界（请先读这段，再看代码）
================================

**这是检查记录，不是签名。**

- 它能证明：**打包工具跑过检查，且检查通过**。
- 它不能证明：**这个 mod 在所有剧情、战斗、存档操作下都没有 BUG**。
- 它也挡不住：**有人把别人的记录字段照抄进自己的包**（无密钥基础设施）。

界面上、文档里都不许把它说成「已认证」「安全」，只能说「已验证过装配」。

三态判据
--------

| 态 | 判据 |
|---|---|
| ``verified`` | 记录存在，且 **两个** 指纹都对得上 |
| ``stale`` | 有记录，但任一指纹不符（代码层更新 / mod 自身改过） |
| ``absent`` | 没有记录 |

为什么必须**两个**指纹
----------------------

早期设计只绑 ``code_digest``（游戏代码层的指纹），有个真漏洞：

    作者在 4.7 上验证通过 → 记录写入 → 作者又改了自己的 mod 代码
    → 游戏基线没变，``code_digest`` 仍匹配 → **旧记录被当成有效**

所以再加一个 ``content_digest``：**扩展自身内容的指纹**。
两个都对上才算 ``verified``。这条是「建议.md」第 2 条指出的，属实。

记录放在哪
----------

**独立文件 ``verification.json``，绝不能写进 ``extension.json``。**

参考启动器（柳江巷 V1.45.1，``tools/compat.py:293``）对清单字段是
**严格白名单**：

    require(not set(m) - known, f"{self.id} 存在未知清单字段：{set(m) - known}")

往清单里加任何新字段，那个启动器会**直接拒绝接入**。历史上作者署名字段
已经踩过一次这个坑。所以：

- 清单格式**一个字节都不动**；
- 验证记录放在扩展包根目录的 ``verification.json``，旧启动器不认识
  这个文件，直接忽略，不影响接入。

本地信任记录
------------

即使有上面的机制，``4.7 首发时 19 个扩展里没有一个带记录`` 是现实 ——
作者不可能先跑一遍再发包（他们连工具都还没有）。所以必须有第二通道：

    config/verified.json —— 玩家点「验证全部」后，本机跑检查通过的结果

它与包内记录**同权**：``verified = 包内记录有效 OR 本地记录有效``。
本地记录同样存两个指纹，所以代码层或 mod 内容一变，同样立即失效。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config as cfg_mod
from .. import paths
from ..fsutil import atomic_write_json, read_json

#: 包内验证记录的文件名。独立于清单，旧启动器会忽略它。
RECORD_NAME = "verification.json"

#: 本地信任记录的文件名（放在 config/ 下）。
LOCAL_NAME = "verified.json"

#: 记录格式版本。改结构时递增，旧记录自动判 ``absent``。
RECORD_SCHEMA = 1

#: 三态。
VERIFIED = "verified"
STALE = "stale"
ABSENT = "absent"


@dataclass
class Verdict:
    """一个扩展的验证判定结果。"""

    ext_id: str
    state: str
    #: 判定依据：``package``（包内记录）/ ``local``（本机记录）/ ``none``
    source: str = "none"
    #: 给玩家看的一句话
    reason: str = ""
    #: 记录里声明的 code_digest（对不上时用于提示「按哪一版代码验的」）
    recorded_code: str = ""
    recorded_content: str = ""

    @property
    def ok(self) -> bool:
        return self.state == VERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.ext_id,
            "state": self.state,
            "source": self.source,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 指纹


def content_digest(root: Path) -> str:
    """扩展包内容的指纹。

    只算「会影响装配结果」的文件：清单、补丁、脚本、数据。**不含**
    ``verification.json`` 自身（否则写入记录会改变自己的指纹，永远对不上）。

    排序后逐个喂 ``相对路径 + 内容哈希``，所以文件增删改名都会变。
    """
    if not root.is_dir():
        return ""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == RECORD_NAME:
            continue          # 记录文件不参与自身指纹
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\n")
    return h.hexdigest()


def code_digest_of(cfg: dict, line: str) -> str:
    """当前这条线的代码层指纹。

    实现放在 :mod:`app.core.baseline_identity`（那里才是基线身份的权威来源），
    这里只做转发，避免两处各算一份、某天算法改了一个忘改另一个。
    """
    from .. import baseline_identity

    return baseline_identity.code_digest_of(cfg, line)


# ---------------------------------------------------------------- 包内记录


def read_record(root: Path) -> dict | None:
    """读扩展包里的验证记录；不存在或格式不对时返回 None。"""
    if not root.is_dir():
        return None
    path = root / RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        schema = int(data.get("schema") or 0)
    except (TypeError, ValueError):
        return None
    if schema != RECORD_SCHEMA:
        return None
    if not str(data.get("code_digest") or "") or not str(data.get("content_digest") or ""):
        return None
    return data


def write_record(
    root: Path,
    *,
    code_digest: str,
    content_digest_value: str,
    line: str,
    passed: list[str],
    tool: str = "jjm_pack",
    tool_version: str = "",
    #: 缺省按「对着目录算的」处理 —— 本函数的 ``root`` 就是个目录，
    #: 调用方默认也在这个目录上算的 content_digest。对 zip 打包的源包
    #: 要显式传 ``"package"``。
    content_scope: str = "dir",
) -> Path:
    """写验证记录到扩展包根目录。

    ``content_digest_value`` 必须由调用方在**写记录之前**算好 ——
    写进去之后再算会把这行记录本身算进指纹，导致校验时永远不符。
    """
    payload = {
        "schema": RECORD_SCHEMA,
        "line": line,
        "code_digest": code_digest,
        "content_digest": content_digest_value,
        # content_digest 是**对着什么**算的：``package``（源 zip，载荷是
        # mods/**）还是 ``dir``（解包目录）。两者文件集合不同，指纹不可比；
        # 判定时必须先看这个字段，否则「装完永远 stale」。
        "content_scope": content_scope,
        "passed": list(passed),
        "tool": tool,
        "tool_version": tool_version,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = root / RECORD_NAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 本地记录


def _local_path() -> Path:
    return paths.CONFIG_DIR / LOCAL_NAME


def load_local() -> dict:
    """读本机信任记录。结构见模块文档。"""
    data = read_json(_local_path(), default=None)
    if not isinstance(data, dict) or int(data.get("schema") or 0) != RECORD_SCHEMA:
        return {"schema": RECORD_SCHEMA, "by_line": {}}
    if not isinstance(data.get("by_line"), dict):
        data["by_line"] = {}
    return data


def save_local(data: dict) -> Path:
    return atomic_write_json(_local_path(), data)


def put_local(
    ext_id: str,
    *,
    line: str,
    code_digest: str,
    content_digest_value: str,
    passed: list[str],
) -> None:
    """把「本机验证通过」写进本地记录。"""
    data = load_local()
    slot = data.setdefault("by_line", {}).setdefault(str(line), {})
    slot[str(ext_id)] = {
        "code_digest": code_digest,
        "content_digest": content_digest_value,
        "passed": list(passed),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_local(data)


def forget_local(ext_id: str, *, line: str) -> None:
    data = load_local()
    slot = data.get("by_line", {}).get(str(line)) or {}
    slot.pop(str(ext_id), None)
    save_local(data)


# ---------------------------------------------------------------- 判定


def judge(
    cfg: dict,
    line: str,
    ext_id: str,
    root: Path | None,
    *,
    code_digest: str | None = None,
    content: str | None = None,
) -> Verdict:
    """判定一个扩展的验证状态。

    ``root`` 是扩展的解包目录（``packs/<id>`` 或临时解包目录）。
    ``code_digest`` / ``content`` 不传时现算。

    判定顺序：包内记录优先，其次本地记录。任一处两个指纹都对得上
    就是 ``verified``；有一处记录但指纹不符是 ``stale``；都没有是 ``absent``。
    """
    want_code = code_digest if code_digest is not None else code_digest_of(cfg, line)

    # 内容指纹**惰性**算 —— 见下方 ``_content``。
    #
    # 原来是进门就 ``content_digest(root)``，对 ``packs/<id>/`` 整棵目录算
    # 一遍 sha256。而覆盖层现在是一整个 pck：``jjxf.avatar_fix`` 的
    # ``overlay-47.pck`` 是 **1.5 GB**，单这一个包就要 1177 ms。
    #
    # 更糟的是这笔开销经常是纯浪费：那个包既没有包内 ``verification.json``、
    # 本地 ``verified.json`` 里也没有它，走的是「两条记录都拿不到 →
    # ``absent``」这条路，算出来的指纹一次都没被读过。
    #
    # 实测（2026-11-14，本机 24 个启用扩展 / 4.7 线）：``judge`` 合计
    # 1515 ms，其中 1177 ms 就是这一个包的 sha256；而 ``gate.plan`` 会
    # 在 ``readiness.evaluate`` 里同步跑在 UI 线程上。
    #
    # 改成「真要比对内容时才算」之后，相等性与副作用完全不变：
    # ``content_digest`` 是纯函数（只读文件、不写盘），延后调用不影响结果。
    _content_cache: list[str] = []

    def _content() -> str:
        if content is not None:
            return content
        if not _content_cache:
            _content_cache.append(content_digest(root) if root else "")
        return _content_cache[0]

    # 无法校验的情况（没有 code_digest）不能算 verified
    if not want_code:
        return Verdict(
            ext_id=ext_id,
            state=STALE if _has_any_record(ext_id, line) else ABSENT,
            reason="还没有 4.7 的代码层指纹，无法判断验证记录是否仍然有效",
        )

    package = read_record(root) if root else None
    local = (load_local().get("by_line", {}).get(str(line)) or {}).get(str(ext_id))

    # ------------------------------------------------------------
    # 包内记录只校 `code_digest`，本地记录才校 `content_digest`。
    #
    # 2026-11-10 修：`seal` 在**源 zip** 上算 content_digest（载荷是
    # `mods/**` 下的脚本与数据），而 `judge` 拿到的 `root` 是**安装后的
    # `packs/<id>/`**（载荷已打包进 `overlay-*.pck`，只剩清单和覆盖层）。
    # 两者文件集合天然不同，指纹永远对不上 —— 于是每个正常 seal 的包
    # 装完都被判 `stale`，4.7 严格档随即剔除。
    #
    # `code_digest` 才是有意义的那个：它绑的是「这条记录是按哪一版
    # 4.7 代码层做的」。4.7 剧情更新后它就会失效，这正是我们想拦的。
    # `content_digest` 的用途是「作者改完自己的包忘了重新 seal」，
    # 那件事在**源包**上才能判 —— 落地到本地记录（`put_local`）时用。
    #
    # 所以：包内记录看 code_digest；本地记录看两者。
    # ------------------------------------------------------------
    for source, rec in (("package", package), ("local", local)):
        if not isinstance(rec, dict):
            continue
        rec_code = str(rec.get("code_digest") or "")
        rec_content = str(rec.get("content_digest") or "")
        if not rec_code or rec_code != want_code:
            continue
        # 比不比内容指纹，取决于**记录是对着什么算的**：
        #   local             —— 装完后在本机算的，root 是同一个目录 → 可比
        #   package + dir     —— 作者对着解包目录 seal 的 → 可比
        #   package + package —— 对着源 zip seal 的，装完文件集合已变 → 不可比
        # 老记录没有 content_scope（向后兼容），按不可比处理。
        comparable = source == "local" or str(rec.get("content_scope") or "") == "dir"
        if comparable:
            want_content = _content()
            if not want_content or rec_content != want_content:
                continue
        return Verdict(
            ext_id=ext_id,
            state=VERIFIED,
            source=source,
            reason="验证记录与当前版本一致",
            recorded_code=rec_code,
            recorded_content=rec_content,
        )

    if package is None and local is None:
        return Verdict(
            ext_id=ext_id,
            state=ABSENT,
            reason="没有验证记录（可能是作者没跑过检查，或不是用本工具打包的）",
        )

    # 有记录但对不上 —— 说清是哪一项变了，别让作者瞎猜
    rec = package if isinstance(package, dict) else local
    rec_code = str((rec or {}).get("code_digest") or "")
    rec_content = str((rec or {}).get("content_digest") or "")
    if rec_code != want_code:
        why = "游戏代码层已更新，这条记录是按旧版本代码做的"
    else:
        why = "扩展自身内容已改动（改完代码后没有重新验证）"
    return Verdict(
        ext_id=ext_id,
        state=STALE,
        source="package" if isinstance(package, dict) else "local",
        reason=why,
        recorded_code=rec_code,
        recorded_content=rec_content,
    )


def _has_any_record(ext_id: str, line: str) -> bool:
    local = (load_local().get("by_line", {}).get(str(line)) or {}).get(str(ext_id))
    return isinstance(local, dict)


def digest_pair(cfg: dict, line: str, root: Path | None) -> tuple[str, str]:
    """一次算好两个指纹，给「写记录」的调用方用。

    顺序很重要：``content_digest`` 必须在**写记录之前**算，
    否则算出来的是「含记录文件的指纹」，写进去之后立刻对不上。
    """
    return code_digest_of(cfg, line), (content_digest(root) if root else "")