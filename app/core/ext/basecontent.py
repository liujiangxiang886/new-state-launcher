"""取「基线里的原始内容」：补丁与钩子插桩都要先拿到原文。

按优先级回落，任一命中即返回：

1. **反解源码工程**（``scripts/x.gd`` 等）——补丁脚本必须用源码，包里只有 ``.gdc``；
2. **剧情覆盖层**（4.7 线，``story_overlay``）——这条线真正的源码/内容层；
3. **基线 pck**（``data/x.json``、``ui/x.tres`` 等，导出包保留了原样文本）；
   本体自带 ``.gd`` 时以它为准——那是玩家这台机器的真实版本；
4. **随包反解源码**（``baseline/canonical_sources.zip``、``baseline/sources-4.7.zip``）
   ——发布包自带，装机即用，玩家不需要另配反解工程。**只在第 3 层拿不到时兜底**：
   4.7 不带源码的导出构建就是靠这一层才能应用文本补丁；
5. **已安装扩展的覆盖层**——「针对另一个扩展的优化补丁」要用这一层。次元裂缝
   优化补丁改的是 ``mods/rose.dimension_ruins/dimension_ruins.gd``，那个文件既
   不在源码根也不在基线里，只能从已装好的次元裂缝覆盖层里取。安装顺序由清单的
   ``requires`` 保证，缺依赖时这里取不到原文，安装会明确报错，不会静默跳过；
6. 都没有 → ``None``，由调用方按 ``on_test_fail`` 决定中止还是跳过。

第 2 层是 1.6 补回来的：1.45.1 把 GDRE 反解结果随包分发，1.4~1.6 重写时丢了，
于是所有 ``text_patches`` 与钩子插桩都因为「基线 pck 里只有 .gdc 没有 .gd」而
拿不到原文。发布包现在把 ``canonical_sources.zip`` 放在 ``baseline/`` 下。

pck 只在第一次真正读取时才打开（38 万条目的目录约 0.6 秒），读完即复用；
canonical zip 同理，只在第一次真正需要时打开。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .. import config as cfg_mod


def canonical_zip(line: str = "4.6") -> Path | None:
    """该引擎线随包携带的反解源码压缩包（没有就返回 ``None``）。

    命名优先线专属（``canonical_sources-46.zip``），退回通用名。多线共存时
    各自一份，不会把 4.6 的源码当成 4.7 的原文用。

    **通用名 ``canonical_sources.zip`` 只给内置线用**：发布包里那份是 1.45
    的源码。4.7 线如果也回落到它，补丁会打在 4.6 的脚本上——``set -e`` 的
    锚点可能照样命中，语义却是错的，比装不上更难查。4.7 的源码来自剧情包。
    """
    from .. import paths

    tag = str(line).replace(".", "")
    base = Path(getattr(paths, "BASELINE", Path("baseline")))
    names = [
        f"canonical_sources-{tag}.zip",
        f"canonical_sources-{line}.zip",
        # 4.7 的随包源码叫这个名字（打包期钩子校验一直用它，运行时过去却
        # 不查它）。发布包里 138 个 .gd 与 4.7 本体 pck 内的源码逐字节一致。
        f"sources-{line}.zip",
        f"sources-{tag}.zip",
    ]
    if line == cfg_mod.BUILTIN_LINE:
        names.append("canonical_sources.zip")
    for name in names:
        path = base / name
        if path.is_file():
            return path
    return None


def story_overlay_of(cfg: dict | None, line: str) -> Path | None:
    """该引擎线的剧情覆盖层（4.7 的源码层 + 内容层）；没有就返回 ``None``。

    数据来源是基线条目里的 ``story_overlay``。它是 4.7 线唯一带 ``.gd``
    源码的地方（原版 4.7 pck 只有 ``.gdc``），所以补丁与插桩必须以它为基底。
    """
    if not isinstance(cfg, dict):
        return None
    baselines = cfg.get("baselines") or {}
    bid = cfg_mod.BASELINE_OF.get(line)
    entry = baselines.get(bid) if bid else None
    if not isinstance(entry, dict):
        entry = next(
            (
                v
                for v in baselines.values()
                if isinstance(v, dict) and str(v.get("engine")) == line
            ),
            None,
        )
    if not isinstance(entry, dict):
        return None
    raw = str(entry.get("story_overlay") or "")
    if not raw:
        return None
    path = cfg_mod.resolve_path(raw)
    return path if path is not None and path.is_file() else None


def canonical_signature(line: str = "4.6", cfg: dict | None = None) -> str:
    """随包源码的签名（文件名 + 大小 + mtime），给插桩缓存做键的一部分。

    传了 ``cfg`` 且本体指纹不符时返回空串：那种情况下这层会被跳过，
    签名必须跟着变，否则会命中「当时还能用」的旧插桩缓存。
    """
    path = canonical_zip(line)
    if path is None:
        return ""
    if cfg is not None and canonical_mismatch(cfg, line):
        return ""
    try:
        st = path.stat()
    except OSError:
        return f"{path.name}:missing"
    return f"{path.name}:{st.st_size}:{st.st_mtime_ns}"


def _canonical_meta(line: str = "4.6") -> dict:
    """随包源码的本体指纹（``canonical_sources.json``）；没有就返回空表。"""
    path = canonical_zip(line)
    if path is None:
        return {}
    # 指纹文件跟着**自己的文件名**走：``canonical_sources.zip`` 配
    # ``canonical_sources.json``，``sources-4.7.zip`` 配 ``sources-4.7.json``。
    # 不能写死 canonical_sources.json——那会拿 1.45 的指纹去校验 4.7 本体，
    # 每次都被判「不符」而把整层源码跳过。
    meta_path = path.with_suffix(".json")
    if not meta_path.is_file():
        return {}
    try:
        import json

        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 指纹坏了不该让源码整体不可用
        return {}
    return data if isinstance(data, dict) else {}


def canonical_mismatch(cfg: dict | None, line: str = "4.6") -> str:
    """随包源码与玩家导入的本体是否对不上；返回空串表示可用。

    为什么需要：这份源码是「1.45 原版」的反解结果。换一个构建（哪怕同为
    1.45）脚本就可能变，补丁锚点仍可能命中，但语义已经不对——装上了、
    跑起来是坏的，比装不上更难查。所以只在能确认是同一份本体时才使用。

    判据按可靠性排序：exe sha256 > 文件大小 > pck 条目数。
    任一项两边都有且不同 → 判为不符；信息缺失不阻断（玩家可能是重新打包的
    同版本本体，宁可让它用，也不要凭空禁用）。
    """
    meta = _canonical_meta(line)
    if not meta or cfg is None:
        return ""

    baselines = cfg.get("baselines") or {}
    bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
    entry = baselines.get(bid)
    if not isinstance(entry, dict):
        entry = next(
            (
                v
                for v in baselines.values()
                if isinstance(v, dict) and str(v.get("engine")) == line
            ),
            None,
        )
    if not isinstance(entry, dict) or not entry:
        return ""   # 还没导入本体：这层本来也用不上

    expected_sha = str(meta.get("exe_sha256") or "").lower()
    actual_sha = str(entry.get("sha256") or "").lower()
    if expected_sha and actual_sha and expected_sha != actual_sha:
        return "本体 sha256 与随包源码不符"

    expected_size = int(meta.get("exe_size") or 0)
    actual_size = int(entry.get("size") or 0)
    if expected_size and actual_size and expected_size != actual_size:
        return "本体大小与随包源码不符"

    # pck 形态的线（4.7）没有 exe 尺寸，用 pck 大小兜底；再往下还有条目数。
    # 两项都留是因为「同尺寸不同内容」几乎不可能，但「同条目数不同大小」
    # 在重新打包过的改版上很常见，只查条目数会漏。
    expected_pck = int(meta.get("pck_size") or 0)
    actual_pck = int(entry.get("pck_size") or 0)
    if expected_pck and actual_pck and expected_pck != actual_pck:
        return "本体 pck 大小与随包源码不符"

    expected_entries = int(meta.get("entry_count") or 0)
    actual_entries = int(entry.get("entry_count") or 0)
    if expected_entries and actual_entries and expected_entries != actual_entries:
        return "本体 pck 条目数与随包源码不符"
    return ""



def _strip(target: str) -> str:
    return target[len("res://"):] if target.startswith("res://") else target.replace("\\", "/")


def _project_engine_line(project_godot: Path) -> str:
    """从反解工程的 ``project.godot`` 读出 Godot 引擎版本，读不出返回空串。

    ``config/features=PackedStringArray("4.7", "GL Compatibility")`` 的首项就是引擎线。
    这是判断「这个反解目录属于哪条线」唯一可靠的依据 —— 目录名、配置里的键名都不可信。
    """
    try:
        text = project_godot.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("config/features"):
            continue
        for token in line.split("=", 1)[-1].split(","):
            token = token.strip().strip('PackedStringArray()[]" ')
            if token and token[0].isdigit():
                return token
    return ""


def source_root(cfg: dict, line: str) -> Path | None:
    """该引擎线的反解工程根目录（没有就返回 None）。

    优先级：**按引擎线匹配的 runtime_sources** → **引擎版本对得上的 reversed_project**。

    历史坑：过去 ``sources.reversed_project`` 无条件排第一，于是不管请求哪条线，
    都拿同一个目录去比对。本机那个目录是 4.7 的，导致 **所有 4.6 线校验都在跟 4.7
    源码比锚点**，一批本来完好的 4.6 包被报成「命中 0 处」——纯假警报。
    """
    meta = (cfg.get("runtime_sources") or {}).get(line) or {}
    project = meta.get("project_godot") or ""
    if project and Path(project).exists():
        return Path(project).parent

    configured = (cfg.get("sources") or {}).get("reversed_project") or ""
    if not configured:
        return None
    root = Path(configured)
    if not root.is_dir():
        return None
    # 目录存在还不够，得确认它真是这条线的源码。读不出引擎版本时保持旧行为
    # （信任配置），避免把没写 features 的合法工程挡在门外。
    actual = _project_engine_line(root / "project.godot")
    if actual and actual != line:
        return None
    return root


#: 随包源码信息缓存：``(路径, 大小, mtime) → info``。算 sha256 要读整个 zip
#: （约 400 KB），每次装包都重算没必要，但改了文件必须失效。
_SOURCE_INFO_CACHE: dict[tuple[str, int, int], dict] = {}


def source_info(line: str = "4.6") -> dict:
    """随包反解源码的元信息：``{name, path, sha256, size}``；没有返回空表。

    ``sha256`` 给清单的 ``sources.sha256`` 做比对——作者声明「我按这份源码
    算的锚点」，安装时拿它和本机随包源码对齐，对不上就拒绝。
    """
    path = canonical_zip(line)
    if path is None:
        return {"name": "", "path": "", "sha256": "", "size": 0}
    try:
        st = path.stat()
    except OSError:
        return {"name": path.name, "path": str(path), "sha256": "", "size": 0}
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _SOURCE_INFO_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    digest = ""
    try:
        from ..hashing import sha256_file

        digest = sha256_file(path)
    except Exception:  # noqa: BLE001 - 算不出来不阻断，只是没法比对
        digest = ""
    info = {
        "name": path.name,
        "path": str(path),
        "sha256": digest,
        "size": st.st_size,
    }
    _SOURCE_INFO_CACHE.clear()
    _SOURCE_INFO_CACHE[key] = info
    return dict(info)


def source_ready(cfg: dict | None, line: str = "4.6") -> tuple[bool, str]:
    """这条线现在能不能拿到**可信的**源码基底。

    返回 ``(可用, 原因)``。原因在不可用时是给人看的一句话，直接进安装失败
    提示，所以要说清「缺什么、怎么补」，不能只写「不可用」。

    顺序与 :class:`BaseContent` 一致：反解工程优先，其次随包源码。
    """
    if cfg is not None and source_root(cfg, line) is not None:
        return True, ""
    path = canonical_zip(line)
    if path is None:
        return False, (
            f"{line} 线没有随包反解源码（baseline/sources-{line}.zip 缺失）。\n"
            "带 text_patches / method_hooks 的扩展必须在源码上打补丁与插桩，"
            "没有源码就只能盲打，补丁可能锚点命中但语义不对。\n"
            "处理：用完整发布包覆盖安装（自带源码），或在设置里指向该线的反解工程。"
        )
    mismatch = canonical_mismatch(cfg, line) if cfg is not None else ""
    if mismatch:
        return False, (
            f"{line} 线的随包源码与本机本体对不上：{mismatch}。\n"
            "这份源码是按标准本体反解的，用在不一致的构建上会让补丁语义错位。\n"
            "处理：换成标准本体，或改用与该本体匹配的反解工程（设置 → 源码目录）。"
        )
    return True, ""


class BaseContent:
    """按需打开的基线内容读取器（上下文管理器可，也可以靠 GC 释放）。"""

    def __init__(
        self,
        cfg: dict | None = None,
        line: str = "4.6",
        exclude: str = "",
        *,
        include_installed: bool = True,
    ) -> None:
        self.cfg = cfg or cfg_mod.load()
        self.line = line
        self.root = source_root(self.cfg, line)
        self._reader = None
        self._failed = False
        #: 正在安装的扩展 id——回落读已装扩展时要跳过自己，否则会读到自己
        #: 上一版留下的旧文件，把补丁打在一个已经过期的基础上。
        self._exclude = exclude
        #: 是否回落读「已装扩展的覆盖层」。
        #: 安装时要（补丁可能针对另一个扩展的文件）；**合并层不要**——
        #: 它要的是原版内容，读回已装扩展等于把上一轮的结果当原文再叠一遍。
        self._include_installed = include_installed
        self._installed: list[tuple[str, object]] | None = None
        #: 随包反解源码（``baseline/canonical_sources.zip``），懒打开。
        self._canon: zipfile.ZipFile | None = None
        self._canon_tried = False
        #: 剧情覆盖层（4.7 的源码/内容层），懒打开。
        self._story = None
        self._story_tried = False
        #: 随包源码被跳过的原因（本体指纹不符），空串表示没跳过。
        self.canonical_skipped = ""

    # ------------------------------------------------------------ 内部

    def _open(self):
        """懒打开该引擎线的基线 pck；打不开就退化为「只有源码/已装扩展」。

        移除 4.7 时这个方法被误删，``read()`` / ``origin()`` 调它会直接
        ``AttributeError``，所有带 data_patches / text_patches 的扩展都会
        在「生成覆盖层」阶段失败。不要删。
        """
        if self._reader is not None or self._failed:
            return self._reader
        from ..pck import PckReader

        bid = next(
            (
                b
                for b, m in (self.cfg.get("baselines") or {}).items()
                if str(m.get("engine")) == self.line
            ),
            None,
        )
        if not bid:
            self._failed = True
            return None
        path = Path((self.cfg["baselines"][bid] or {}).get("path") or "")
        if not path.exists():
            self._failed = True
            return None
        try:
            self._reader = PckReader.open(path)
        except Exception:  # noqa: BLE001 - 读不了就退化为「只有源码」
            self._failed = True
            return None
        return self._reader

    # ------------------------------------------------------------ 对外

    def _canonical(self) -> zipfile.ZipFile | None:
        """懒打开随包反解源码；没有/损坏都按「这一层不存在」处理。"""
        if self._canon_tried:
            return self._canon
        self._canon_tried = True
        path = canonical_zip(self.line)
        if path is None:
            return None
        reason = canonical_mismatch(self.cfg, self.line)
        if reason:
            # 本体对不上：宁可退回基线 pck（拿不到脚本原文 → 明确失败），
            # 也不要把补丁打到一个语义不同的脚本上。
            self.canonical_skipped = reason
            return None
        try:
            self._canon = zipfile.ZipFile(path)
        except Exception:  # noqa: BLE001 - 坏包退化为下一层，不阻断安装
            self._canon = None
        return self._canon

    def _from_canonical(self, rel: str) -> bytes | None:
        zf = self._canonical()
        if zf is None:
            return None
        try:
            return zf.read(rel)
        except KeyError:
            return None
        except Exception:  # noqa: BLE001 - 加密/损坏都按「拿不到」处理
            return None

    def _open_story(self):
        """懒打开剧情覆盖层；没有/损坏都按「这一层不存在」处理。

        4.7 线的原版 pck 只有 ``.gdc``，剧情包才是 ``.gd`` 源码的载体。
        补丁与钩子插桩都必须以它为基底，否则会拿 4.6 的源码去改 4.7 的包。
        """
        if self._story_tried:
            return self._story
        self._story_tried = True
        path = story_overlay_of(self.cfg, self.line)
        if path is None:
            return None
        try:
            from ..pck import PckReader

            self._story = PckReader.open(path)
        except Exception:  # noqa: BLE001 - 坏包退化为下一层
            self._story = None
        return self._story

    def read(self, target: str) -> bytes | None:
        rel = _strip(target)
        if not rel:
            return None
        # 1) 反解工程（源码形态，打补丁优先用它）
        if self.root is not None:
            candidate = self.root / rel
            if candidate.is_file():
                try:
                    return candidate.read_bytes()
                except OSError:
                    pass
        # 2) 剧情覆盖层（4.7 的源码/内容层）——比随包源码更贴近这条线的真实内容
        story = self._open_story()
        if story is not None:
            try:
                entry = story.get(rel)
                if entry is not None:
                    return story.read_bytes(entry)
            except Exception:  # noqa: BLE001 - 读不到就落下一层
                pass
        # 3) 基线 pck。**排在随包源码前面**：玩家本体里若已带 ``.gd``，那才是
        #    他这台机器的真实版本；随包的 canonical 只在「本体只有 .gdc」时兜底。
        #    反过来（先 canonical）会在本体自带源码、但构建略有差异时，
        #    拿我们那份去改别人的包。
        reader = self._open()
        if reader is not None:
            entry = reader.get(rel)
            if entry is not None:
                try:
                    return reader.read_bytes(entry)
                except Exception:  # noqa: BLE001 - 加密/损坏都按「拿不到」处理
                    pass
        # 4) 随包反解源码（``canonical_sources.zip`` / ``sources-4.7.zip``）
        data = self._from_canonical(rel)
        if data is not None:
            return data
        # 5) 已装扩展的整文件副本
        return self._from_installed(rel)

    def _installed_readers(self) -> list[tuple[str, object]]:
        """已安装扩展的覆盖层阅读器（跳过正在装的自己）。"""
        if not self._include_installed:
            return []
        if self._installed is not None:
            return self._installed
        self._installed = []
        from .. import paths
        from ..pck import PckReader

        packs = getattr(paths, "PACKS", None)
        if packs is None or not Path(packs).is_dir():
            return self._installed
        tag = paths.line_tag(self.line)
        for d in sorted(Path(packs).iterdir()):
            if not d.is_dir() or d.name == self._exclude:
                continue
            pck = d / f"overlay-{tag}.pck"
            if not pck.is_file():
                continue
            try:
                self._installed.append((d.name, PckReader.open(pck)))
            except Exception:  # noqa: BLE001 - 坏包不该拦住别的扩展装
                continue
        return self._installed

    def _from_installed(self, rel: str) -> bytes | None:
        for _name, rd in self._installed_readers():
            try:
                entry = rd.get(rel)
            except Exception:  # noqa: BLE001
                continue
            if entry is None:
                continue
            try:
                return rd.read_bytes(entry)
            except Exception:  # noqa: BLE001
                continue
        return None

    def compiled_only(self, target: str) -> bool:
        """基线里只有该脚本的 ``.gdc`` 字节码、没有 ``.gd`` 源码吗？

        只用于**诊断文案**：4.7 线的原版 pck 若是不带源码的构建，补丁类扩展
        会一个都打不上，界面却只说「目标基线里没有这个文件」，玩家无从判断
        是自己包错了还是本体不对。这里给出可执行的判断依据。
        """
        rel = _strip(target)
        if not rel.endswith(".gd"):
            return False
        reader = self._open()
        if reader is None:
            return False
        try:
            return reader.get(rel + "c") is not None
        except Exception:  # noqa: BLE001 - 判断失败按「不是」处理
            return False

    def origin(self, target: str) -> str:
        rel = _strip(target)
        if self.root is not None and (self.root / rel).is_file():
            return "source"
        story = self._open_story()
        if story is not None:
            try:
                if story.get(rel) is not None:
                    return "story"
            except Exception:  # noqa: BLE001
                pass
        reader = self._open()
        if reader is not None and reader.get(rel) is not None:
            return "baseline"
        if self._from_canonical(rel) is not None:
            return "canonical"
        if self._from_installed(rel) is not None:
            return "extension"
        return "missing"

    def close(self) -> None:
        if self._canon is not None:
            try:
                self._canon.close()
            except Exception:  # noqa: BLE001
                pass
        self._canon = None
        self._canon_tried = False
        self._story = None
        self._story_tried = False
        self._reader = None
        self._installed = None
