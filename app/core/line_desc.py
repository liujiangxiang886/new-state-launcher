"""自定义版本描述（``line.json``）：创作者声明能力，启动器负责验证。

边界（与需求方对齐）
--------------------
反解工程、源码、能力/锚点索引都由**改版创作者自备**，随改版一起分发；
启动器只给运行壳与接入壳，不逆向、不猜、不替作者补全没声明的能力。

启动器只做三件事：

1. 按 schema 读描述；
2. 校验描述绑定的 ``identity.identity_hash`` == 身份闸门对该文件算出的指纹
   （防止把 4.7 的描述挂到另一份改版上）；
3. **逐项**校验描述声明的方法/信号/锚点，在创作者提供的反解工程索引里
   真的存在。

第 3 条是这套框架的核心部件、不是可选项：只信声明不验证，就等于绕回
「改版的玩法与扩展的钩子互不相识，硬挂只会崩」。校验不过就降级到
``probe-only``（能启动、只读探测，不挂扩展），并把原因写进 ``access_note``。

索引格式（创作者产出，``source_index`` 指向它）
----------------------------------------------
``{"files": [...], "symbols": {"scripts/game.gd": ["combat_power", ...]},
"anchors": {"scripts/game.gd": ["func combat_power(", ...]}}``
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 描述文件名：放在改版本体同目录，随改版一起分发
DESC_FILENAME = "line.json"
SCHEMA_VERSION = 1

TIER_FULL = "full"
TIER_PROBE_ONLY = "probe-only"

_REQUIRED = ("schema", "name", "version", "identity", "source_index", "capabilities")
_KINDS_NEED_INDEX = ("method", "signal", "hook")


class LineDescError(ValueError):
    """描述文件本身不合规（缺字段、JSON 坏、版本不认识）。"""


#: 自定义版本描述里禁止声明的联机能力 / 协议词。
#: 口径与 ``ext/netguard.py`` 的 DECLARED_CAPS + P2P_BLOCK_RULES 一致：
#: ENet / WebSocket / UDP / TCP / DTLS 直连、自建服务端、IP:端口、
#: NAT 穿透、局域网发现，以及 online / multiplayer / p2p / 联机 这类自述。
#: 这里查的是「创作者声明本线提供什么」，不是「代码里有没有」——
#: 声明了就不给接入，避免第三方改版把联机能力从接入壳带进来。
NETWORK_TOKENS = (
    "online", "multiplayer", "netplay", "coop", "p2p", "联机", "联网",
    "多人", "开房", "房间", "组队", "对战",
    "enet", "websocket", "ws://", "wss://", "udp://", "tcp://",
    "packetpeer", "streampeer", "multiplayerpeer", "multiplayerapi",
    "udpserver", "tcpserver", "dtls", "punch", "landiscovery",
    "create_server", "create_client", "create_mesh", "peer_to_peer",
    "resolve_hostname", "socket",
)


def network_declarations(desc: dict[str, Any]) -> list[str]:
    """扫描述的能力表，返回命中的联机声明片段（空 = 干净）。"""
    chunks: list[str] = []
    for cap in desc.get("capabilities") or []:
        if isinstance(cap, dict):
            chunks.extend(str(v) for v in cap.values() if v)
        elif cap:
            chunks.append(str(cap))
    found: list[str] = []
    for chunk in chunks:
        low = chunk.lower().replace("_", "").replace("-", "")
        for token in NETWORK_TOKENS:
            if token.lower() in low:
                found.append(f"{chunk}（命中 {token}）")
                break
    return found


@dataclass
class DescReport:
    """一致性校验结果；``errors`` 非空一律降级，不阻断启动。"""

    ok: bool
    tier: str = TIER_PROBE_ONLY
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    index_files: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def desc_path_for(game_file: str | Path) -> Path:
    """描述文件默认与改版本体同目录。"""
    return Path(game_file).parent / DESC_FILENAME


def load_desc(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LineDescError(f"描述文件读不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise LineDescError("描述文件顶层必须是对象")
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise LineDescError(f"描述缺字段：{', '.join(missing)}")
    if data.get("schema") != SCHEMA_VERSION:
        raise LineDescError(f"不认识的 schema 版本：{data.get('schema')!r}（应为 {SCHEMA_VERSION}）")
    if not isinstance(data.get("capabilities"), list):
        raise LineDescError("capabilities 必须是数组")
    if data.get("network") is True:
        raise LineDescError("本启动器不支持联机接入：network 必须为 false 或省略")
    return data


def _load_index(desc_dir: Path, rel: str) -> dict[str, Any]:
    if not rel or str(rel).strip() in (".", "..") or Path(rel).is_absolute():
        raise LineDescError(f"source_index 必须是描述文件同目录内的相对路径：{rel!r}")
    p = desc_dir / rel
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LineDescError(f"反解工程索引读不出来：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise LineDescError("反解工程索引必须含 files 数组")
    return data


def validate_desc(desc: dict[str, Any], verdict, desc_dir: str | Path) -> DescReport:
    """校验「描述 ↔ 闸门身份 ↔ 创作者反解工程索引」三者一致。"""
    errors: list[str] = []
    warnings: list[str] = []
    caps: list[str] = []

    ident = desc.get("identity") or {}
    if not isinstance(ident, dict):
        errors.append("identity 必须是对象")
        ident = {}
    for key, actual, label in (
        ("identity_hash", getattr(verdict, "identity_hash", ""), "身份指纹"),
        ("engine_line", getattr(verdict, "engine_line", ""), "引擎线"),
        ("baseline", getattr(verdict, "baseline", ""), "内置基准"),
    ):
        if ident.get(key) != actual:
            errors.append(f"描述的 {label} 与文件不符：{ident.get(key)!r} ≠ {actual!r}")

    try:
        index = _load_index(Path(desc_dir), str(desc.get("source_index") or ""))
    except LineDescError as exc:
        errors.append(str(exc))
        return DescReport(False, errors=errors, capabilities=caps)

    # 不支持联机接入：描述里声明了联机能力/协议，整条线不升级。
    net_hits = network_declarations(desc)
    if net_hits:
        errors.append(
            "描述声明了联机能力或协议，本启动器不支持联机接入：" + "；".join(net_hits[:3])
        )
        return DescReport(
            False, errors=errors,
            capabilities=[str(c.get("id")) for c in desc["capabilities"]
                          if isinstance(c, dict) and c.get("id")],
        )

    files = set(str(x) for x in index["files"])
    symbols = index.get("symbols") or {}
    anchors = index.get("anchors") or {}
    if not symbols:
        warnings.append("反解索引没有 symbols 表，方法/信号类能力无法证明存在")

    for cap in desc["capabilities"]:
        if not isinstance(cap, dict) or not cap.get("id"):
            errors.append(f"能力条目缺 id：{cap!r}")
            continue
        cid = str(cap["id"])
        caps.append(cid)
        kind = str(cap.get("kind") or "")
        if kind == "resource":
            # 纯资源挂载（覆盖层）不碰代码，只要线本身能挂 pck 即可
            continue
        if kind not in _KINDS_NEED_INDEX:
            warnings.append(f"能力 {cid} 的 kind={kind!r} 未列入需要索引的类型")
            continue
        target = str(cap.get("target") or "")
        if not target:
            errors.append(f"能力 {cid} 缺 target（脚本路径）")
            continue
        if target not in files:
            errors.append(f"能力 {cid} 指向的 {target} 不在反解索引的 files 里")
            continue
        symbol = str(cap.get("symbol") or cid.rsplit(".", 1)[-1])
        available = set(str(x) for x in (symbols.get(target) or []))
        if symbol not in available:
            errors.append(f"能力 {cid} 声明的 {symbol} 不在 {target} 的反解符号表里")

    for anchor in desc.get("anchors") or []:
        if not isinstance(anchor, dict):
            errors.append(f"锚点条目格式不对：{anchor!r}")
            continue
        aid, target = str(anchor.get("id") or "?"), str(anchor.get("file") or "")
        text = str(anchor.get("anchor") or "")
        if target not in files:
            errors.append(f"锚点 {aid} 指向的 {target} 不在反解索引的 files 里")
            continue
        available = [str(x) for x in (anchors.get(target) or [])]
        if text and not any(text in a for a in available):
            errors.append(f"锚点 {aid} 的文本在 {target} 的反解锚点表里找不到")

    ok = not errors
    return DescReport(
        ok=ok,
        tier=TIER_FULL if ok else TIER_PROBE_ONLY,
        errors=errors,
        warnings=warnings,
        capabilities=caps,
        index_files=len(files),
    )


def try_upgrade(game_file: str | Path, verdict) -> tuple[str, str]:
    """给 ``custom_lines`` 用：有描述且校验通过才升到 ``full``。

    返回 ``(tier, access_note)``。任何失败都只降级、不抛错、不阻断启动——
    创作者没交描述是常态，不是错误。
    """
    p = desc_path_for(game_file)
    if not p.is_file():
        return TIER_PROBE_ONLY, "未提供 line.json：仅启动 + 只读探测"
    try:
        desc = load_desc(p)
    except LineDescError as exc:
        return TIER_PROBE_ONLY, f"描述不可用：{exc}"
    report = validate_desc(desc, verdict, p.parent)
    if report.ok:
        note = f"描述校验通过（{len(report.capabilities)} 项能力 / {report.index_files} 个索引文件）"
    else:
        note = "描述未通过一致性校验：" + "；".join(report.errors[:3])
    return report.tier, note
