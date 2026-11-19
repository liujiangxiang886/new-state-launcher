"""Godot 引擎调用：Autoload 推导、导入扫描、导出。

计划 §4.5 要求「Autoload 推导算法」，§4.8 要求「增强路径：检测到编辑器时
``--headless --export-pack``」。两件事都是「怎么跟 Godot 命令行打交道」，
放一起。

设计取舍：

- **不用 GDScript 干这些**：它们都是「读文件 + 跑进程」，Python 更合适，
  也不受引擎版本差异影响；
- **导出默认关闭**：模板 exe 编译时没带导出功能，真正的
  ``--export-pack`` 需要 1.2 GB export_templates。检测不到就明确说「不可用」，
  而不是假装能做；
- **Autoload 推导可解释**：每个入围/淘汰的候选都给出理由，方便人工核对。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..errors import EngineMissing, LauncherError

#: Godot 内建类名（出现在「标识符.方法」里时不能当作 Autoload）
BUILTIN_CLASSES = {
    "Engine", "OS", "ProjectSettings", "Input", "InputMap", "DisplayServer",
    "AudioServer", "PhysicsServer2D", "PhysicsServer3D", "RenderingServer",
    "NavigationServer2D", "Time", "Marshalls", "JSON", "FileAccess", "DirAccess",
    "ResourceLoader", "ResourceSaver", "Image", "ImageTexture", "FontFile",
    "ThemeDB", "TranslationServer", "ClassDB", "Geometry2D", "Geometry3D",
    "Performance", "WorkerThreadPool", "JavaClassWrapper", "JavaScriptBridge",
    "Tween", "SceneTree", "Node", "Node2D", "Node3D", "Control", "CanvasItem",
    "CanvasLayer", "Viewport", "Window", "PackedScene", "Resource", "Object",
    "RefCounted", "Signal", "Callable", "Variant", "String", "StringName",
    "Array", "Dictionary", "Vector2", "Vector3", "Color", "Rect2", "Transform2D",
    "Transform3D", "Basis", "Quaternion", "AABB", "Plane", "RandomNumberGenerator",
    "Crypto", "HashingContext", "XMLParser", "StreamPeerTCP", "HTTPRequest",
    "HTTPClient", "Timer", "MainLoop", "EditorPlugin", "EditorInterface",
    "AudioStreamPlayer", "PackedByteArray", "PackedStringArray", "GlobalScope",
}

#: 4.6 契约里的 Autoload 顺序（推导时作为并列时的排序依据）
CONTRACT_ORDER = (
    "Game", "Controls", "FlashTextEffects", "ItemTips",
    "SkillTips", "Audio", "Social", "Pets",
)

#: ``extends`` 里允许的基类。
#: Autoload 必须是**节点**，但节点类很多：``Window`` 是 ``Viewport`` 的子类，
#: ``CharacterBody2D`` 是 ``PhysicsBody2D → CollisionObject2D → Node2D`` 的子类，
#: 都属于合法 Autoload。按「是否继承 Node」判断代替白名单穷举：
#: 这里列出常见的直接基类，其余靠 NODE_BASE_HINTS 的后缀匹配兜底。
NODE_BASES = (
    "Node", "Node2D", "Node3D", "Control", "CanvasLayer", "Window",
    "Viewport", "Spatial", "Resource",
)

#: 名字里带这些片段的基类基本都是节点（如 CharacterBody2D / Area2D / Sprite2D）
NODE_BASE_HINTS = (
    "Body2D", "Body3D", "Area2D", "Area3D", "Sprite", "Label", "Button",
    "Panel", "Container", "Item", "TileMap", "Camera", "Light", "Particles",
    "Animation", "AudioStream", "Navigation", "Path", "Line", "Polygon",
    "RigidBody", "StaticBody", "CollisionShape", "CollisionPolygon",
)


def _is_node_base(base: str) -> bool:
    """基类是否属于「节点」谱系。

    ``RefCounted`` / ``Resource`` 这类不是节点，不能当 Autoload——它们没有
    ``_ready`` 生命周期，也不会挂到场景树上。
    """
    if not base:
        return False
    if base.startswith("Resource") or base == "RefCounted":
        return False
    if any(base == b or base.startswith(b) for b in NODE_BASES):
        # Resource 已在上面排除，这里剩下的都是节点谱系
        return base != "Resource"
    return any(hint in base for hint in NODE_BASE_HINTS)

_AUTOLOAD_RE = re.compile(r"^([A-Z][A-Za-z0-9_]*)\s*=\s*\"\*?(res://[^\"]+)\"", re.M)
_REF_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([a-zA-Z_][A-Za-z0-9_]*)\s*\(")


@dataclass
class AutoloadCandidate:
    name: str
    script: str
    base: str = ""
    refs: int = 0
    accepted: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "script": self.script,
            "base": self.base,
            "refs": self.refs,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass
class AutoloadPlan:
    candidates: list[AutoloadCandidate] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "order": self.order,
            "candidates": [c.as_dict() for c in self.candidates],
            "notes": self.notes,
        }

    def to_project_lines(self, root: str = "res://") -> list[str]:
        """生成 ``project.godot`` 的 ``[autoload]`` 行（Compat 在最前）。"""
        lines = ['Compat="*res://_compat/bootstrap.gd"']
        for candidate in self.candidates:
            if candidate.accepted:
                lines.append(f'{candidate.name}="*{candidate.script}"')
        return lines


# ---------------------------------------------------------------- 项目文件


def read_project(path: str | Path) -> dict:
    """读 ``project.godot``（INI 风格），返回 ``{section: {key: value}}``。

    只做够用级别的解析：不处理多行值与非标准转义——我们只需要 autoload、
    application、input 这几段，它们都是简单的 ``key="value"``。
    """
    file = Path(path)
    if not file.is_file():
        raise LauncherError(f"project.godot 不存在：{file}", path=str(file))
    out: dict[str, dict[str, str]] = {}
    section = ""
    for raw in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" in line and section:
            key, _, value = line.partition("=")
            out[section][key.strip()] = value.strip()
    return out


def parse_autoloads(project: dict) -> list[tuple[str, str]]:
    """从已解析的 project 里取 Autoload 列表 ``[(name, script)]``。

    值的真实形态是 ``"*res://scripts/game.gd"``——**引号在最外层**，
    星号在引号里面。所以必须先剥引号再剥星号；顺序反了就一个都剥不掉
    （这里踩过一次，表现为「8 个 Autoload 全部被判成脚本不存在」）。
    """
    out: list[tuple[str, str]] = []
    for key, value in (project.get("autoload") or {}).items():
        script = value.strip()
        if script.startswith('"') and script.endswith('"'):
            script = script[1:-1]
        script = script.strip().strip('"').strip("'")
        if script.startswith("*"):
            script = script[1:]
        script = script.strip()
        if script:
            out.append((key, script))
    return out


def write_autoload_block(text: str, lines: list[str]) -> str:
    """把 ``[autoload]`` 段整体替换成给定行（保留其它段）。"""
    block = "[autoload]\n\n" + "\n".join(lines) + "\n"
    pattern = re.compile(r"\[autoload\][^\[]*", re.S)
    if pattern.search(text):
        return pattern.sub(block + "\n", text, count=1)
    return text.rstrip() + "\n\n" + block


# ---------------------------------------------------------------- 推导


def _script_bases(root: Path) -> dict[str, str]:
    """扫描工程里每个 ``.gd`` 的 ``extends`` 基类。"""
    out: dict[str, str] = {}
    for path in root.rglob("*.gd"):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        match = re.search(r"^\s*extends\s+([A-Za-z0-9_\.]+)", head, re.M)
        if match:
            out[path.relative_to(root).as_posix()] = match.group(1)
    return out


def _reference_counts(root: Path) -> dict[str, int]:
    """统计每个「大写标识符.方法(...)」出现次数。"""
    counts: dict[str, int] = {}
    for path in root.rglob("*.gd"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _REF_RE.finditer(text):
            name = match.group(1)
            if name in BUILTIN_CLASSES:
                continue
            counts[name] = counts.get(name, 0) + 1
    return counts


def derive_autoloads(project_root: str | Path, *, keep_contract: bool = True) -> AutoloadPlan:
    """推导 Autoload 列表。

    算法（计划 §4.5）：

    1. 扫全部 ``.gd`` 的「大写标识符.方法」引用；
    2. 剔除 Godot 内建类名与 ``preload`` 别名；
    3. 保留「存在同名 ``.gd`` 且 ``extends`` 是节点类」的候选；
    4. ``Compat`` 固定第 1，其余按 4.6 契约顺序 + 引用次数排序；
    5. ``FlashTextEffects`` 保留（删除收益为零、风险非零）。
    """
    root = Path(project_root)
    plan = AutoloadPlan()
    if not root.is_dir():
        plan.notes.append(f"工程目录不存在：{root}")
        return plan

    project_file = root / "project.godot"
    declared: list[tuple[str, str]] = []
    if project_file.is_file():
        declared = parse_autoloads(read_project(project_file))
        plan.notes.append(f"project.godot 里已声明 {len(declared)} 个 Autoload")

    bases = _script_bases(root)
    refs = _reference_counts(root)

    # 以「声明过的」为底，再补充「有引用但没声明」的
    seen: set[str] = set()
    entries: list[tuple[str, str]] = list(declared)
    for name, count in sorted(refs.items(), key=lambda kv: -kv[1]):
        if name in seen or any(n == name for n, _ in entries):
            continue
        guess = f"res://scripts/{_snake(name)}.gd"
        if (root / guess[len("res://"):]).is_file():
            entries.append((name, guess))

    for name, script in entries:
        rel = script.replace("res://", "")
        base = bases.get(rel, "")
        candidate = AutoloadCandidate(
            name=name,
            script=script,
            base=base,
            refs=refs.get(name, 0),
        )
        if not (root / rel).is_file():
            candidate.reason = "脚本文件不存在"
        elif base and not _is_node_base(base):
            candidate.reason = f"基类 {base} 不是节点类（没有场景树生命周期）"
        else:
            candidate.accepted = True
            candidate.reason = "声明过" if any(n == name for n, _ in declared) else "被引用且存在同名脚本"
        seen.add(name)
        plan.candidates.append(candidate)

    # 覆盖兜底：契约里的 8 个若缺了，标记出来（不强行加入）
    if keep_contract:
        present = {c.name for c in plan.candidates if c.accepted}
        missing = [n for n in CONTRACT_ORDER if n not in present]
        if missing:
            plan.notes.append(f"4.6 契约里有但本工程不可用的：{', '.join(missing)}")
        if "FlashTextEffects" in missing:
            plan.notes.append("FlashTextEffects 按计划保留：删除收益为零、风险非零")

    # 排序：契约顺序优先，其次引用次数，最后名字
    order_index = {name: i for i, name in enumerate(CONTRACT_ORDER)}

    def sort_key(candidate: AutoloadCandidate) -> tuple[int, int, str]:
        return (
            order_index.get(candidate.name, len(CONTRACT_ORDER)),
            -candidate.refs,
            candidate.name,
        )

    plan.order = [c.name for c in sorted((c for c in plan.candidates if c.accepted), key=sort_key)]
    plan.candidates.sort(key=sort_key)
    return plan


def _snake(name: str) -> str:
    """``FlashTextEffects`` → ``flash_text_effects``。"""
    out = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return out


# ---------------------------------------------------------------- 引擎调用


def version(exe: str | Path, timeout: float = 30) -> str:
    """取引擎版本串（``--version``）。"""
    path = Path(exe)
    if not path.is_file():
        raise EngineMissing(f"引擎不存在：{path}", path=str(path))
    proc = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (proc.stdout or proc.stderr or "").strip()


def is_editor(exe: str | Path) -> bool:
    """是编辑器构建还是导出模板。

    判据：模板的 ``--help`` 里没有 ``--export-pack``（编译时就去掉了），
    而且模板会带 ``.pck`` 同名配对行为。这里用 ``--help`` 的输出判断更稳。
    """
    path = Path(exe)
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (proc.stdout or "") + (proc.stderr or "")
    return "--export-pack" in text or "--export-release" in text


def import_project(
    exe: str | Path, project_dir: str | Path, *, timeout: float = 1800
) -> tuple[bool, str]:
    """跑一次 ``--headless --import``，让引擎补齐 ``.godot/imported``。

    这是路线 A 的验收动作：能 import 通过，说明工程结构完整。
    """
    path = Path(exe)
    if not path.is_file():
        raise EngineMissing(f"引擎不存在：{path}", path=str(path))
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return False, f"不是 Godot 工程：{proj}"
    try:
        proc = subprocess.run(
            [str(path), "--headless", "--path", str(proj), "--import"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, f"import 超时（{timeout:.0f}s）"
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    return proc.returncode == 0, f"rc={proc.returncode} " + " | ".join(tail)


def export_pack(
    exe: str | Path, project_dir: str | Path, out: str | Path, *, timeout: float = 1800
) -> tuple[bool, str]:
    """``--export-pack`` 导出（需要编辑器构建 + export_templates）。

    模板构建上不可用，会明确返回失败原因，不假装成功。
    """
    path = Path(exe)
    if not is_editor(path):
        return False, "该引擎是导出模板，不支持 --export-pack（需要编辑器构建 + export_templates）"
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                str(path), "--headless", "--path", str(project_dir),
                "--export-pack", "Windows Desktop", str(out_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, f"导出超时（{timeout:.0f}s）"
    if proc.returncode == 0 and out_path.is_file():
        return True, f"导出成功：{out_path}（{out_path.stat().st_size:,} 字节）"
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    return False, f"rc={proc.returncode} " + " | ".join(tail)


def scan_missing_resources(project_dir: str | Path, packed_paths: frozenset[str]) -> dict:
    """扫工程里引用了但打包产物里没有的资源。

    计划 §4.4 路线 C 的验收判据是「缺失清单为空」。这里用静态文本扫描：
    把 ``.tscn``/``.tres``/``.gd``/``.json`` 里的 ``res://`` 引用全部收集起来，
    与 pck 里的路径集合比对。

    会**跳过**三类非真实引用（否则清单永远清不干净）：

    - 格式化占位符：``"res://scripts/%s.gd" % name``、``career_%d.png``
    - 目录前缀：``"res://assets/ui/xxx/" + file``（以 ``/`` 结尾）
    - 明显是拼接用的片段（含 ``{``、``%``、``$``）
    """
    root = Path(project_dir)
    refs: dict[str, set[str]] = {}
    pattern = re.compile(r'res://([^"\'\s\)]+)')
    for suffix in ("*.tscn", "*.tres", "*.gd", "*.json"):
        for path in root.rglob(suffix):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            for match in pattern.finditer(text):
                target = match.group(1)
                if _is_dynamic_ref(target):
                    continue
                refs.setdefault(target, set()).add(rel)

    missing = {
        target: sorted(users)[:5]
        for target, users in refs.items()
        if target not in packed_paths and not _possibly_compiled(target, packed_paths)
    }
    return {
        "total_refs": len(refs),
        "skipped_dynamic": _SKIPPED[0],
        "missing": missing,
        "missing_count": len(missing),
    }


#: 记录被跳过的动态引用数量（``scan_missing_resources`` 会写）
_SKIPPED = [0]


def _is_dynamic_ref(target: str) -> bool:
    """是否是运行期拼出来的路径（不是静态资源引用）。"""
    if not target:
        _SKIPPED[0] += 1
        return True
    # 目录前缀：以 / 结尾，后面接文件名
    if target.endswith("/"):
        _SKIPPED[0] += 1
        return True
    # 格式化 / 插值占位符
    if any(ch in target for ch in "%{}$"):
        _SKIPPED[0] += 1
        return True
    return False


def _possibly_compiled(target: str, packed: frozenset[str]) -> bool:
    """引用源文件但包里只有编译产物时视为「不缺」。

    导出包的形态是「源文件 → 编译产物 + remap」，所以三种情况都算命中：

    - ``x.png`` → 有 ``x.png.import``（贴图/音频/字体）
    - ``x.gd``  → 有 ``x.gdc`` 或 ``x.gd.remap``（脚本）
    - ``x.tscn`` → 有 ``.godot/exported/*/export-<md5>-<名>.scn`` 或 ``x.tscn.remap``
    - ``x.tres`` → 有 ``x.tres.remap`` 或 ``.godot/exported/*/*.res``

    最后一类无法按名字精确对应（导出名带 md5），只按后缀判断：
    只要包里存在 ``.scn`` / ``.res`` 就认为这类资源可能已编译。
    宁可漏报（缺失清单偏短）也不要误报——误报会让这个门禁失去意义。
    """
    if f"{target}.import" in packed or f"{target}.remap" in packed:
        return True
    if target.endswith(".gd") and f"{target}c" in packed:
        return True
    if target.endswith((".tscn", ".tres", ".res")):
        return any(p.startswith(".godot/exported/") for p in packed)
    # 源资源（图片/字体/音频）：.import 在就行，或同名 ctex 已导入
    if f"{target}.import" in packed:
        return True
    name = Path(target).name
    prefix = ".godot/imported/"
    return any(p.startswith(prefix) and p.split(prefix, 1)[1].startswith(f"{name}-") for p in packed)


def engine_report() -> dict:
    """两代引擎的版本与能力（诊断页用）。"""
    out: dict[str, object] = {}
    from .. import config as cfg_mod

    cfg = cfg_mod.load()
    for line, meta in (cfg.get("engines") or {}).items():
        path = cfg_mod.resolve_path(meta.get("path") or "") or Path("")
        item: dict[str, object] = {"path": str(path), "exists": path.is_file()}
        if path.is_file():
            try:
                item["version"] = version(path)
            except (OSError, subprocess.TimeoutExpired) as exc:
                item["version"] = f"读取失败：{exc}"
            item["editor"] = is_editor(path)
            item["bytes"] = path.stat().st_size
        out[line] = item
    return out