"""游戏本体（基线）的探测、校验、登记与索引重建。

为什么单独一层
--------------

``library._import_game_exe`` 过去只往配置里写一个 ``path``，而
``config.baseline_exists`` 是按 ``size`` 判存在的——于是「导入成功」的下一秒
就被判成「文件缺失」。这里把「识别 → 校验 → 落配置 → 建索引」收成一条链，
界面、CLI、测试共用同一套结论。

三条硬规则
------------------------------------------

- 4.6（唯一内置基线，对应原版 1.45）：exe（内嵌 pck）或独立 pck 都收，
  ``pack_version`` 必须是 3；
- 读出来 0 个条目 → 不是游戏数据包，直接拒；
- ``custom:*`` 不走这里（自定义线不解析 pck、不绑基线）。

``rebuild_index`` 刻意不 import ``tools/``：发布包不带 ``tools/``，而导入流程
在玩家机器上必须能自己把索引建出来。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg_mod, paths
from .hashing import sha256_file
from .pck import PckReader, embed
from .pck import format as pfmt

#: 基线 id → 引擎线（与 :data:`app.core.config.BASELINE_OF` 互逆）。
#: 发布者版同时登记原版 1.45（4.6）与剧情版 4.7（4.7）两条线。
LINE_OF_BASELINE = {v: k for k, v in cfg_mod.BASELINE_OF.items()}


@dataclass(frozen=True)
class BaselineProbe:
    """一个候选本体文件的识别结果。只读，不持有文件句柄。"""

    path: Path
    kind: str                 # "pck" | "exe"
    size: int                 # 整文件字节数（exe 含 PE stub）
    pck_size: int             # 数据包字节数（exe 时是内嵌段）
    pack_version: int         # 3 / 4
    engine_version: tuple[int, int, int]
    entry_count: int
    sha256: str = ""

    @property
    def engine_line(self) -> str | None:
        return pfmt.engine_line_for(self.pack_version)

    @property
    def engine_text(self) -> str:
        return ".".join(str(x) for x in self.engine_version)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "size": self.size,
            "pck_size": self.pck_size,
            "pack_version": self.pack_version,
            "engine": self.engine_text,
            "engine_line": self.engine_line,
            "entry_count": self.entry_count,
            "sha256": self.sha256,
        }


@dataclass
class LineIssue:
    level: str      # error | warn | info
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class IndexReport:
    baseline_id: str
    line: str
    index_path: Path
    identity_path: Path
    entry_count: int
    seconds: float
    index_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "baseline_id": self.baseline_id,
            "line": self.line,
            "index": str(self.index_path),
            "identity": str(self.identity_path),
            "entry_count": self.entry_count,
            "index_bytes": self.index_bytes,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


# ---------------------------------------------------------------- 探测


def probe(
    path: str | Path,
    *,
    count_entries: bool = True,
    sha256: bool = False,
    progress=None,
) -> BaselineProbe:
    """识别一个文件是 pck 还是内嵌 pck 的 exe，并读出头部与条目数。

    ``count_entries=False`` 只读头部（毫秒级），用于「先看看是什么」；
    ``sha256=True`` 会整文件读一遍（3 GB 约 10–30 秒），导入落配置时再算。
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    size = p.stat().st_size
    if size < 16:
        raise ValueError(f"文件太小，不可能是游戏数据包：{p}")

    with p.open("rb") as f:
        head = f.read(4)
    if head == b"GDPC":
        kind = "pck"
        reader = PckReader.open(p)
    elif embed.has_pck(p):
        kind = "exe"
        reader = PckReader.from_exe(p)
    else:
        raise ValueError(
            "这个文件既不是 .pck（头部不是 GDPC），"
            "也不是内嵌 pck 的 exe（尾部没有 GDPC 标记）。"
        )

    entries = len(reader) if count_entries else 0
    if progress:
        progress(60, f"已识别：{kind}，{entries:,} 条" if count_entries else "已读取头部")
    digest = ""
    if sha256:
        digest = sha256_file(p)
        if progress:
            progress(95, "哈希计算完成")

    return BaselineProbe(
        path=p.resolve(),
        kind=kind,
        size=size,
        pck_size=int(reader.header.pck_size),
        pack_version=int(reader.header.pack_version),
        engine_version=tuple(reader.header.engine_version),  # type: ignore[arg-type]
        entry_count=entries,
        sha256=digest,
    )


# ---------------------------------------------------------------- 校验


def validate(p: BaselineProbe, line: str) -> list[LineIssue]:
    """按引擎线校验一个探测结果。4.6 读 v3、4.7 读 v4。"""
    issues: list[LineIssue] = []
    expected = pfmt.PACK_VERSION_BY_ENGINE.get(line)
    if expected is None:
        return [
            LineIssue(
                "error",
                "baseline.line",
                f"未知引擎线：{line}（支持 {'、'.join(pfmt.PACK_VERSION_BY_ENGINE)}）",
            )
        ]

    if p.entry_count == 0:
        issues.append(
            LineIssue("error", "baseline.empty", "读出来 0 个条目，这个文件不是游戏数据包。")
        )

    if p.pack_version != expected:
        hint = (
            "1.45 请选 mchGame.exe（或 pack_version=3 的 pck）。"
            if line == cfg_mod.BUILTIN_LINE
            else f"{line} 剧情版请选 pack_version=4 的 pck。"
        )
        issues.append(
            LineIssue(
                "error",
                "baseline.pack_version",
                f"这个文件是 {p.pack_version} 版数据包"
                f"（引擎 {p.engine_text}），不能登记为 {line} 线基线。{hint}",
            )
        )
    if p.kind == "pck":
        issues.append(
            LineIssue(
                "warn",
                "baseline.needs_exe",
                "登记的是独立 pck：启动时还需要一个配套 exe（与 pck 同目录、同名）。"
                f"没有也没关系，{line} 会用启动器自带的引擎。",
            )
        )
    return issues


def can_adopt(p: BaselineProbe, line: str) -> bool:
    return not any(i.level == "error" for i in validate(p, line))


def blocking(issues: list[LineIssue]) -> list[LineIssue]:
    return [i for i in issues if i.level == "error"]


# ---------------------------------------------------------------- 校验状态

#: 基线校验状态。**只有明确写成 ``pending`` / ``failed`` 才会拦启动。**
#:
#: 为什么必须把「字段缺失」与 ``pending`` 分开：1.5.8x 之前的配置里没有
#: 这个字段。升级后把「没字段」一律判成待校验，等于所有老玩家一开就
#: 「无法启动」——那是比「导入后能立刻启动」更糟的结果。所以缺字段按
#: 「不拦」处理，只有走过新导入流程、被明确标成待校验的才拦。
VERIFY_PENDING = "pending"
VERIFY_VERIFIED = "verified"
VERIFY_FAILED = "failed"

#: 这些状态一律不允许启动。
BLOCKING_VERIFY_STATES = frozenset({VERIFY_PENDING, VERIFY_FAILED})

#: 状态 → 界面用中文。
VERIFY_STATE_LABELS: dict[str, str] = {
    VERIFY_PENDING: "待校验",
    VERIFY_VERIFIED: "已校验通过",
    VERIFY_FAILED: "校验未通过",
}

#: ``verify_by`` 的取值：是谁把状态置成 verified 的。
VERIFY_BY_SMOKE = "smoke"
VERIFY_BY_INDEX = "index"
#: 后台校验任务整体崩了（既不是冒烟结论也不是索引结论）。
VERIFY_BY_TASK = "task"


def verify_state(cfg: dict, baseline_id: str) -> str:
    """读基线校验状态；配置里没有这条基线时返回空串。"""
    meta = (cfg.get("baselines") or {}).get(baseline_id)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("verify_state") or "")


def is_launchable(cfg: dict, baseline_id: str) -> bool:
    """这条基线现在允不允许启动。

    只有 ``pending`` / ``failed`` 拦；字段缺失（老配置）与 ``verified``
    都放行 —— 迁移策略见 :data:`VERIFY_PENDING` 的注释。
    """
    return verify_state(cfg, baseline_id) not in BLOCKING_VERIFY_STATES


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slot_of(cfg: dict, baseline_id: str) -> dict:
    """取（必要时创建）基线槽位。

    必须用 ``setdefault``，不能写 ``cfg.get("baselines") or {}`` ——
    空字典是 falsy，那样取到的是**一个新的临时字典**，setdefault 写进
    临时对象里，原配置一点没变。表现是「标记已校验后仍然不能启动」，
    而且 ``is_launchable`` 读不到字段（返回空串）还会把它误判成放行。
    """
    baselines = cfg.setdefault("baselines", {})
    slot = baselines.get(baseline_id)
    if not isinstance(slot, dict):
        slot = {}
        baselines[baseline_id] = slot
    return slot


def mark_pending(cfg: dict, baseline_id: str, *, note: str = "") -> dict:
    """置「待校验」。

    **必须在 save 之前调用**：否则程序崩溃/玩家关窗会留下一个
    「文件登记了、状态没写」的基线，重启后又变成可以直接启动。
    """
    slot = _slot_of(cfg, baseline_id)
    slot["verify_state"] = VERIFY_PENDING
    slot["verify_at"] = ""
    slot["verify_by"] = ""
    slot["verify_note"] = note
    return slot


def mark_verified(cfg: dict, baseline_id: str, *, by: str, note: str = "") -> dict:
    """置「已校验通过」。``by`` 说明靠什么验的：``smoke`` / ``index``。"""
    slot = _slot_of(cfg, baseline_id)
    slot["verify_state"] = VERIFY_VERIFIED
    slot["verify_at"] = _stamp()
    slot["verify_by"] = by
    slot["verify_note"] = note
    return slot


def mark_failed(cfg: dict, baseline_id: str, *, by: str, note: str = "") -> dict:
    """置「校验未通过」。这种状态也不允许启动 —— 玩家能看清是哪个文件不行。"""
    slot = _slot_of(cfg, baseline_id)
    slot["verify_state"] = VERIFY_FAILED
    slot["verify_at"] = _stamp()
    slot["verify_by"] = by
    slot["verify_note"] = note
    return slot


def verify_summary(cfg: dict, baseline_id: str) -> str:
    """一行话：状态 + 时间 + 方式 + 备注。给日志/工具提示用。"""
    meta = (cfg.get("baselines") or {}).get(baseline_id) or {}
    state = str(meta.get("verify_state") or "")
    if not state:
        return "未校验（旧配置，不拦启动）"
    parts = [VERIFY_STATE_LABELS.get(state, state)]
    if meta.get("verify_at"):
        parts.append(str(meta["verify_at"]))
    if meta.get("verify_by"):
        parts.append(f"方式 {meta['verify_by']}")
    if meta.get("verify_note"):
        parts.append(str(meta["verify_note"]))
    return " · ".join(parts)


# ---------------------------------------------------------------- 登记


def adopt(
    cfg: dict,
    baseline_id: str,
    p: BaselineProbe,
    *,
    sibling_exe: Path | None = None,
    keep_sha256_if_size_same: bool = True,
) -> dict:
    """把探测结果写进配置（只改内存，调用方负责 save）。

    返回写后的 baseline 字典。``sha256`` 没算时：大小没变就沿用旧值
    （3 GB 重算一次 10–30 秒，不值得），变了就清空并标 ``sha256_stale``，
    避免拿旧哈希去校验新文件。

    **登记同时把 ``verify_state`` 置为 ``pending``**：调用方 save 之后，
    :func:`app.core.readiness.evaluate` 会判这条基线「不能启动」，直到
    校验通过并调用 :func:`mark_verified`。见 :data:`VERIFY_PENDING`。
    """
    baselines = cfg.setdefault("baselines", {})
    slot = baselines.setdefault(baseline_id, {})
    old_size = slot.get("size")

    slot["kind"] = p.kind
    slot["path"] = cfg_mod.stored_path(p.path)
    slot["size"] = int(p.size)
    slot["entry_count"] = int(p.entry_count)
    slot["pack_version"] = int(p.pack_version)
    slot["engine"] = LINE_OF_BASELINE.get(baseline_id, p.engine_line or "")
    slot["pck_size"] = int(p.pck_size)

    if p.sha256:
        slot["sha256"] = p.sha256
        slot.pop("sha256_stale", None)
    elif keep_sha256_if_size_same and old_size == p.size and slot.get("sha256"):
        pass  # 同一个文件，旧哈希仍然有效
    else:
        slot["sha256"] = ""
        slot["sha256_stale"] = True

    if p.kind == "pck":
        exe = sibling_exe or p.path.with_suffix(".exe")
        if exe.is_file():
            slot["exe_path"] = cfg_mod.stored_path(exe)
            slot["exe_size"] = exe.stat().st_size
        else:
            slot.pop("exe_path", None)
            slot.pop("exe_size", None)
    else:
        # exe 自带数据，配套 pck 字段没有意义
        slot.pop("exe_path", None)
        slot.pop("exe_size", None)

    # **登记即待校验。**
    #
    # 2026-11-16 玩家事故换来的：旧流程「先写配置 → 再可选跑冒烟」，
    # 导入完成那一秒就能启动；3 GB 的包还没校验完就点启动，游戏读到
    # 半成品 pck，26 秒后 0xC0000005 崩掉（launcher-20261116-113827）。
    #
    # 为什么写在这里而不是交给界面：adopt 是「登记本体」的唯一入口，
    # 界面暂时只有两个调用方，以后 CLI 也会走它。状态跟登记绑在一起，
    # 才不会出现「某个入口忘了置 pending」。
    slot["verify_state"] = VERIFY_PENDING
    slot["verify_at"] = ""
    slot["verify_by"] = ""
    slot["verify_note"] = ""

    return slot


# ---------------------------------------------------------------- 索引


def short_id(baseline_id: str) -> str:
    """"jjxf.v1.45" -> "1.45"，用于索引文件名。

    非 ``jjxf.v*`` 的 id（``custom:1`` 之类）会把 Windows 文件名非法字符
    换成 ``-``——索引文件名带冒号在 Windows 上根本创建不出来。
    """
    m = re.match(r"^jjxf\.v(.+)$", baseline_id)
    name = m.group(1) if m else baseline_id
    return re.sub(r'[\\/:*?"<>|\s]+', "-", name)


def write_index(reader: PckReader, dest: Path) -> str:
    """分块写 gzip JSON（移植自 tools/make_index.py，发布包不 import tools）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    digest = hashlib.sha256()
    header = {
        "schema": 1,
        "source": str(reader.path),
        "header": reader.header.summary(),
        "entry_count": len(reader.entries),
    }
    with gzip.open(tmp, "wb", compresslevel=6) as gz:

        def emit(text: str) -> None:
            data = text.encode("utf-8")
            digest.update(data)
            gz.write(data)

        head_json = json.dumps(header, ensure_ascii=False)
        assert head_json.endswith("}")
        emit(head_json[:-1])
        emit(',"entries":[')

        buf: list[str] = []
        first = True

        def flush() -> None:
            nonlocal first
            if not buf:
                return
            chunk = ",".join(buf)
            emit(chunk if first else "," + chunk)
            buf.clear()
            first = False

        for entry in reader.entries:
            buf.append(json.dumps(entry.as_row(), ensure_ascii=False, separators=(",", ":")))
            if len(buf) >= 5000:
                flush()
        flush()
        emit("]}")
    tmp.replace(dest)
    return digest.hexdigest()


def rebuild_index(
    line: str | None = None,
    *,
    baseline_ids: list[str] | None = None,
    progress=None,
) -> list[IndexReport]:
    """就地重建基线索引与身份文件。

    ``line`` 给定时只重建该引擎线对应的基线；``baseline_ids`` 优先。
    索引先写 ``.tmp`` 再 replace，中途失败不会破坏旧索引。
    """
    cfg = cfg_mod.load()
    if baseline_ids:
        ids = list(baseline_ids)
    elif line:
        ids = [bid for bid, meta in (cfg.get("baselines") or {}).items()
               if str((meta or {}).get("engine")) == line]
    else:
        ids = list((cfg.get("baselines") or {}).keys())

    paths.ensure_dirs()
    reports: list[IndexReport] = []
    for index, baseline_id in enumerate(ids, start=1):
        meta = (cfg.get("baselines") or {}).get(baseline_id) or {}
        src = cfg_mod.resolve_path(str(meta.get("path") or ""))
        started = time.time()
        report = IndexReport(
            baseline_id=baseline_id,
            line=LINE_OF_BASELINE.get(baseline_id, str(meta.get("engine") or "")),
            index_path=paths.BASELINE / f"index-{short_id(baseline_id)}.json.gz",
            identity_path=paths.BASELINE / f"identity-{short_id(baseline_id)}.json",
            entry_count=0,
            seconds=0.0,
        )
        if src is None or not src.is_file():
            report.errors.append(f"文件不存在：{src}")
            reports.append(report)
            continue
        if progress:
            progress(int(100 * (index - 1) / max(len(ids), 1)), f"重建 {baseline_id} 索引…")
        try:
            reader = PckReader.open(src)
            index_sha = write_index(reader, report.index_path)
            report.entry_count = len(reader)
            report.index_bytes = report.index_path.stat().st_size

            # **保留已有的分层身份字段。**
            #
            # 这段原先是「用新字典整个覆盖写 identity」，后果是：
            # 只要点一次「重建索引」，`layers`（body/exe 整包 sha256）和
            # `code_layer`（代码层 digest + 252 个文件的哈希）就被抹掉。
            #
            # 那两样是**验证标志的锚**：`code_layer.digest` 就是
            # `code_digest`，抹掉之后启动器算不出指纹，所有扩展一律被判
            # 「无法验证」，完全校验档形同虚设；而且 ``jjm_pack seal``
            # 也会因为拿不到 digest 拒绝出记录。
            #
            # 实测踩过一次：给 4.7 重建索引，identity 从 41 KB 掉到 1.3 KB。
            # 所以这里改成「读旧的 → 合并」：索引相关的字段用新算的，
            # 分层字段原样保留。文件坏了读不出来时退回全新生成，不报错。
            previous: dict = {}
            try:
                if report.identity_path.is_file():
                    previous = json.loads(
                        report.identity_path.read_text(encoding="utf-8")
                    )
                    if not isinstance(previous, dict):
                        previous = {}
            except (OSError, ValueError):
                previous = {}

            identity = {
                "schema": 1,
                "id": baseline_id,
                "label": meta.get("label", ""),
                "kind": meta.get("kind", ""),
                "engine_line": meta.get("engine", ""),
                "path": str(src),
                "size": src.stat().st_size,
                "sha256": meta.get("sha256", ""),
                "header": reader.header.summary(),
                "entry_count": len(reader),
                "total_entry_bytes": sum(e.size for e in reader.entries),
                "encrypted_entries": sum(1 for e in reader.entries if e.encrypted),
                "top_suffixes": reader.suffixes(12),
                "index_file": report.index_path.name,
                "index_sha256": index_sha,
                "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # 分层字段：旧文件有就用旧的（那是 freeze_baseline 精确算出来的，
            # 重建索引顺便重算会多花 10 秒以上，且算法不同源容易漂移）
            for keep_key in ("layers", "code_layer"):
                if previous.get(keep_key):
                    identity[keep_key] = previous[keep_key]

            report.identity_path.write_text(
                json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - 单个基线失败不影响其它
            report.errors.append(f"{type(exc).__name__}: {exc}")
        report.seconds = time.time() - started
        reports.append(report)

    invalidate_caches()
    if progress:
        progress(100, "索引重建完成")
    return reports


def invalidate_caches() -> None:
    """清掉 baseline 模块的 lru_cache（换了 path/size 后必须调）。"""
    from . import baseline as baseline_mod

    for name in ("_suffix_for_line", "load_index", "path_set"):
        func = getattr(baseline_mod, name, None)
        clear = getattr(func, "cache_clear", None)
        if callable(clear):
            clear()
