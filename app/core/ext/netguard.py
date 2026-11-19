"""联机模块闸门：拒绝携带 P2P / 直连网络协议的扩展。

1.5 只负责启动单机游戏。扩展作者如果往包里塞 P2P 联机代码，等于用
玩家的机器直连别人的机器——这条口子不开放。这里做的是**协议层**拦截，
不是"看到联机两个字就拒"：

- 阻断：点对点传输协议与直连寻址（ENet / UDP / TCP / WebSocket 直连、
  自建服务器、IP:端口、协议 URL、多人对等端赋值）。
- 只告警：``@rpc`` 注解、联机相关文案。Godot 里没有对等端时 RPC 不生效，
  单机脚本也可能预留注解，按阻断处理会误杀正常扩展。

例外：启动器不逆向扫描改版本体自身携带什么能力（3GB 的第三方包，
也不该由我们替作者判定）。但**接入壳不等于放开联机**：自定义线的
``line.json`` 描述由 ``line_desc.network_declarations`` 按本模块同一套口径
检查，经协商挂载的扩展清单与 overlay 正文由 ``negotiate`` 走
``scan_manifest`` / ``scan_bytes`` 复查。闸门只检查扩展与描述，
不读、不改、不限制改版本体。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: 参与扫描的文本后缀。二进制资源（贴图/音频/字库）不扫，避免误判。
TEXT_SUFFIXES = (
    ".gd",
    ".gdscript",
    ".json",
    ".txt",
    ".md",
    ".cfg",
    ".ini",
    ".tscn",
    ".tres",
    ".shader",
    ".gdshader",
    ".csv",
    ".yaml",
    ".yml",
)

#: 单文件扫描上限。超过就只看头尾，防止作者塞一个巨型资源拖死安装。
MAX_SCAN_BYTES = 2 * 1024 * 1024

#: 硬阻断规则：(正则, 说明)。命中即拒绝安装。
P2P_BLOCK_RULES: tuple[tuple[str, str], ...] = (
    (r"\bENetMultiplayerPeer\b", "ENet 点对点传输"),
    (r"\bWebSocketMultiplayerPeer\b", "WebSocket 联机传输"),
    (r"\bMultiplayerAPI\b", "多人对等 API"),
    (r"multiplayer\s*\.\s*multiplayer_peer\s*=", "设置多人对等端"),
    (r"\bPacketPeerUDP\b", "UDP 点对点直连"),
    (r"\bPacketPeerDTLS\b", "DTLS 点对点直连"),
    (r"\bStreamPeerTCP\b", "TCP 直连"),
    (r"\b(?:UDPServer|TCPServer)\b", "自建网络服务端"),
    (r"\bWebSocketPeer\b", "WebSocket 直连"),
    (r"\bIP\s*\.\s*resolve_hostname\b", "主机名解析（联机寻址）"),
    (r"\b(?:create_server|create_client|create_mesh)\s*\(", "ENet 建链"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}\b", "IP:端口直连地址"),
    (r"\b(?:wss?|tcp|udp|enet)://", "网络协议地址"),
    (r"\b(?:nat_)?punch_?through\b", "NAT 穿透"),
    (r"\blan_?discover(?:y)?\b", "局域网发现"),
)

#: 只告警的规则：命中不阻断，但会在安装结果里记一条。
P2P_WARN_RULES: tuple[tuple[str, str], ...] = (
    (r"@rpc\b", "RPC 注解（无对等端时不生效）"),
    (r"\brpc_id\s*\(", "RPC 调用（无对等端时不生效）"),
    (r"联机|开房|组队|公会战|快速匹配|匹配房间|房间列表", "联机相关文案"),
)

#: 清单 capabilities / features 里出现这些词，视为作者自述携带联机能力。
DECLARED_CAPS = (
    "online",
    "multiplayer",
    "netplay",
    "coop",
    "p2p",
    "联机",
)

_BLOCK_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in P2P_BLOCK_RULES]
_WARN_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in P2P_WARN_RULES]

#: 命中片段两侧保留多少字符，方便作者定位。
_EXCERPT = 40


@dataclass
class NetHit:
    """一条命中记录。"""

    rule: str
    reason: str
    where: str
    excerpt: str = ""
    level: str = "block"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "rule": self.rule,
            # ``reason`` 是这条判定的主文案；``message`` 是给统一 issue 渲染
            # 用的别名——界面按 message 取文本，只给 reason 会打出
            # 「注意：None」（2026-11-13 实测）。
            "reason": self.reason,
            "message": self.reason,
            "where": self.where,
            "excerpt": self.excerpt,
        }

    def line(self) -> str:
        head = f"[{self.level}] {self.reason} @ {self.where}"
        return f"{head}\n        命中片段：{self.excerpt}" if self.excerpt else head


@dataclass
class NetReport:
    """一个扩展包的联机扫描结果。"""

    hits: list[NetHit] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(h.level == "block" for h in self.hits)

    @property
    def blocks(self) -> list[NetHit]:
        return [h for h in self.hits if h.level == "block"]

    @property
    def warnings(self) -> list[NetHit]:
        return [h for h in self.hits if h.level == "warn"]

    def message(self) -> str:
        """给玩家看的拒绝原因（不出现 Python 术语）。"""
        if not self.blocked:
            return ""
        first = self.blocks[0]
        more = len(self.blocks) - 1
        tail = f"（另有 {more} 处）" if more else ""
        return (
            f"该扩展携带联机模块（{first.reason}），1.5 启动器不支持联机，已拒绝安装。"
            f"{tail}如确需联机，请使用自带联机功能的游戏版本。"
        )

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "hits": [h.as_dict() for h in self.hits],
        }


def _excerpt(text: str, match: re.Match) -> str:
    start = max(0, match.start() - _EXCERPT)
    end = min(len(text), match.end() + _EXCERPT)
    return text[start:end].replace("\r", "").replace("\n", " ⏎ ").strip()


def _scan_text(report: NetReport, where: str, text: str) -> None:
    for pattern, why in _BLOCK_RE:
        for m in pattern.finditer(text):
            report.hits.append(
                NetHit(rule=pattern.pattern, reason=why, where=where,
                       excerpt=_excerpt(text, m), level="block")
            )
            break
    for pattern, why in _WARN_RE:
        for m in pattern.finditer(text):
            report.hits.append(
                NetHit(rule=pattern.pattern, reason=why, where=where,
                       excerpt=_excerpt(text, m), level="warn")
            )
            break


def _scan_bytes(report: NetReport, where: str, data: bytes) -> None:
    if len(data) > MAX_SCAN_BYTES:
        data = data[:MAX_SCAN_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="ignore")
    _scan_text(report, where, text)


def _declared(caps: object) -> list[str]:
    """把 capabilities 里的各种写法摊平成小写字符串。"""
    if caps is None:
        return []
    if isinstance(caps, str):
        return [caps.lower()]
    if isinstance(caps, (list, tuple, set)):
        out: list[str] = []
        for item in caps:
            if isinstance(item, str):
                out.append(item.lower())
            elif isinstance(item, dict):
                out.extend(str(v).lower() for v in item.values())
        return out
    if isinstance(caps, dict):
        return [str(v).lower() for v in caps.values()]
    return []


def _scan_manifest(report: NetReport, data: dict) -> None:
    caps = []
    for key in ("capabilities", "features"):
        caps.extend(_declared(data.get(key)))
    for cap in caps:
        for word in DECLARED_CAPS:
            if word in cap:
                report.hits.append(
                    NetHit(rule=f"declared:{word}", reason="清单自述携带联机能力",
                           where=f"extension.json:{cap}", level="block")
                )
                break

    # 补丁正文是"将要写进游戏"的代码，必须按代码扫。
    for key in ("text_patches", "data_patches"):
        payload = json.dumps(data.get(key) or [], ensure_ascii=False)
        if payload not in ("[]", ""):
            _scan_text(report, f"extension.json<{key}>", payload)

    for key in ("scripts", "scenes", "assets", "minigames"):
        payload = json.dumps(data.get(key) or [], ensure_ascii=False)
        if payload not in ("[]", ""):
            _scan_text(report, f"extension.json<{key}>", payload)


def _text_suffix(name: str) -> bool:
    return name.lower().endswith(TEXT_SUFFIXES)


def scan_manifest(data: dict | None) -> NetReport:
    """只扫清单声明（不碰包体），给协商层这类没有原包的调用点用。

    ``_scan_manifest`` 是内部实现；对外给一个稳定入口，避免调用方去碰
    私有函数，也避免为了扫声明而伪造一个包路径。
    """
    report = NetReport()
    if isinstance(data, dict):
        _scan_manifest(report, data)
    return report


def scan_bytes(data: bytes, where: str = "<memory>") -> NetReport:
    """扫一段内容里的联机协议痕迹（自定义线的 overlay pck 条目走这里）。"""
    report = NetReport()
    _scan_bytes(report, where, data)
    return report


def scan_package(package: str | Path, manifest: dict | None = None) -> NetReport:
    """扫描一个扩展包（zip 或已解包目录）。

    ``manifest`` 可由调用方传入已经解析好的清单，避免重复读取。
    """
    report = NetReport()
    pkg = Path(package)
    data = manifest

    if pkg.is_file():
        try:
            with zipfile.ZipFile(pkg) as zf:
                if data is None:
                    try:
                        data = json.loads(zf.read("extension.json").decode("utf-8"))
                    except (KeyError, ValueError):
                        data = None
                if data:
                    _scan_manifest(report, data)
                for info in zf.infolist():
                    if info.is_dir() or not _text_suffix(info.filename):
                        continue
                    if info.filename == "extension.json":
                        continue
                    try:
                        _scan_bytes(report, info.filename, zf.read(info))
                    except (KeyError, OSError, RuntimeError):
                        continue
        except zipfile.BadZipFile:
            report.hits.append(
                NetHit(rule="bad_zip", reason="压缩包无法读取", where=str(pkg), level="block")
            )
        return report

    if pkg.is_dir():
        if data is None:
            try:
                data = json.loads((pkg / "extension.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
        if data:
            _scan_manifest(report, data)
        for path in sorted(pkg.rglob("*")):
            if not path.is_file() or not _text_suffix(path.name):
                continue
            if path.name == "extension.json":
                continue
            # 只扫包内相对路径，跳过作者可能顺手放进来的构建缓存。
            rel = path.relative_to(pkg).as_posix()
            if "__pycache__" in rel or rel.startswith("."):
                continue
            try:
                _scan_bytes(report, rel, path.read_bytes())
            except OSError:
                continue
        return report

    report.hits.append(
        NetHit(rule="missing", reason="扩展包不存在", where=str(pkg), level="block")
    )
    return report


def audit_manifest_only(data: dict) -> NetReport:
    """只看清单，用于已经装上、包里没有原文件的旧扩展。"""
    report = NetReport()
    _scan_manifest(report, data)
    return report
