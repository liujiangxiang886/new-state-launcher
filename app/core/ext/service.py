"""扩展服务：安装 / 验证 / 注册 / 卸载。

这是界面与拖拽共用的**统一扩展接口**。一次安装分六步，每步都通过
``progress(percent, label)`` 回调上报，界面的进度状态栏直接消费它：

1. 读取清单（只读，不碰基线）
2. 静态校验（文件齐不齐、钩子能不能落到真实源码上）
3. 解包载荷
4. 生成覆盖层 pck（覆盖基线脚本时自动补自指 remap）
5. 回读校验（条目数 / 大小 / md5）
6. 挂载冒烟（把基线 + 覆盖层真的挂起来，headless 实例化主场景）

只有 6 通过才算 ``verified``。带 ``method_hooks`` 的包会被标成
``needs_hooks``——文件层装好了，但钩子插桩还没实现，界面上必须说清楚，
不能让人以为已经能玩。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config as cfg_mod
from .. import messages as msg_mod
from .. import paths, runner
from ..errors import LauncherError
from ..fsutil import ArchivePathTooLongError, atomic_write_json, is_longpath_error, read_json
from ..hashing import sha256_file
from ..pck import PckReader
from . import apply as ext_apply
from . import decisions as decisions_mod
from . import hooks as hooks_mod
from . import isolation as isolation_mod
from . import manifest as ext_manifest
from . import netguard
from .basecontent import BaseContent

Progress = Callable[[int, str], None]

#: 注册表位置（已安装扩展清单）
REGISTRY = paths.CONFIG_DIR / "extensions.json"

STATUS_READY = "ready"            # 文件层完整，可挂载
STATUS_NEEDS_HOOKS = "needs_hooks"  # 还缺方法钩子插桩

#: 玩家版普通模式仍要拒掉的「不适配启动器」错误码。
#:
#: 其它静态检查（文件清单、钩子目标、补丁命中）在这个模式下**全部跳过**：
#: 玩家只关心「能不能装、能不能玩」，作者的实现细节不该变成安装门槛。
#: 但下面这几类如果放过去，装上也必然跑不起来，属于「不适配」而不是「不完美」。
_LAUNCHER_FATAL = frozenset(
    {
        "schema.unsupported",   # 启动器读不懂的清单版本
        "id.invalid",           # 注册表都记不住的身份
        "files.unsafe_target",  # 写到包外的路径（安全底线，任何模式都不放）
    }
)


def _loose_mode(cfg: dict) -> bool:
    """当前是不是宽松档。

    **宽松不等于不校验** —— 两档跑的是同一套检查（锚点 / units / 钩子 /
    成员），区别只在处置：宽松只提示、照装。这个函数只用于「要不要额外
    强制跑一次挂载冒烟」这类**耗时**决策，不用来跳过任何判定。
    """
    return not cfg_mod.is_strict(cfg_mod.extension_mode(cfg))


def _show_hook_info(mode: str, in_bundle: bool) -> bool:
    """是否在安装结果里提「钩子」。

    产品约定：

    - 完全校验：始终提，作者需要知道钩子有没有落到源码上。
    - 宽松校验：单个扩展不提，只有导入整合包（合集）时才汇总提示 ——
      玩家装单个包时不想看实现细节，装整合包时才需要知道哪些没生效。
    """
    if cfg_mod.is_strict(mode):
        return True
    return in_bundle


@dataclass
class InstallResult:
    ok: bool
    ext_id: str = ""
    name: str = ""
    version: str = ""
    engine_line: str = ""
    baseline: str = ""
    status: str = ""
    overlay: Path | None = None
    entry_count: int = 0
    sha256: str = ""
    verified: bool = False
    message: str = ""
    #: 稳定的失败码（``install.overlay`` 等），界面据此选人话文案
    code: str = ""
    #: 技术细节：只进日志/诊断包，不展示给玩家
    detail: str = ""
    #: 追加说明（例如「已作废本线插桩缓存 N 份」），拼在 message 之后
    message_extra: str = ""
    issues: list[dict] = field(default_factory=list)
    #: 装完与「已启用组合」比对出的问题（只保留这个包参与的）。
    #: 这是「导入时就告诉用户会不会冲突」的落点，不再等启动才发现。
    conflicts: list[dict] = field(default_factory=list)
    #: 扩展隔离扫描结果（跨扩展访问、钩子撞车、死代码 context 键…）
    isolation: list[dict] = field(default_factory=list)
    #: 被完全校验档拒绝（``strict`` 下存在 block 级违规）
    isolation_blocked: bool = False
    #: 跨线安装被拒（清单声明的线 ≠ 目标线）
    line_mismatch: bool = False
    #: 被拒时清单声明的引擎线
    declared_line: str = ""
    #: 普通模式下按顺序自动取舍的位置（不阻断安装）
    degraded: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "id": self.ext_id,
            "name": self.name,
            "version": self.version,
            "engine_line": self.engine_line,
            "baseline": self.baseline,
            "status": self.status,
            "overlay": str(self.overlay) if self.overlay else None,
            "entry_count": self.entry_count,
            "sha256": self.sha256,
            "verified": self.verified,
            "message": self.message,
            "code": self.code,
            "detail": self.detail,
            "issues": self.issues,
            "conflicts": self.conflicts,
            "isolation": self.isolation,
            "isolation_blocked": self.isolation_blocked,
            "line_mismatch": self.line_mismatch,
            "declared_line": self.declared_line,
            "degraded": self.degraded,
        }


class ExtensionService:
    """面向界面的扩展门面。线程安全的前提是不共享可变状态，一次装一个。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        #: 最近一次运行校验的完整结果（含 mod 装载明细），供界面出可读提示
        self._last_probe = None
        #: 最近一次探针声明的插桩点个数（0 表示没有声明，或上一次是纯资源包）
        self._probe_hook_count = 0

    # ------------------------------------------------------------ 注册表

    def installed(self) -> list[dict]:
        data = read_json(REGISTRY, default={"schema": 1, "items": []}) or {}
        items = data.get("items") or []
        return [i for i in items if isinstance(i, dict)]

    def _save_registry(self, items: list[dict]) -> None:
        atomic_write_json(REGISTRY, {"schema": 1, "items": items})

    def get(self, ext_id: str) -> dict | None:
        return next((i for i in self.installed() if i.get("id") == ext_id), None)

    def _stored_findings(self, item: dict | None) -> list[dict] | None:
        """取出安装时存下的隔离扫描结果（没有则返回 None）。"""
        if not item:
            return None
        lines = item.get("lines")
        if isinstance(lines, dict):
            for entry in lines.values():
                if isinstance(entry, dict) and isinstance(entry.get("isolation"), list):
                    return [f for f in entry["isolation"] if isinstance(f, dict)]
        iso = item.get("isolation")
        if isinstance(iso, list):
            return [f for f in iso if isinstance(f, dict)]
        return None

    def _rescan_isolation(self, item: dict) -> list[dict] | None:
        """源码还在时现扫一次（老版本装的扩展没有存档结果）。"""
        ext_id = str(item.get("id") or "")
        line = str(item.get("engine_line") or "")
        if not ext_id or not line:
            return None
        src = paths.BUILDS / f"_install_{ext_id}_{paths.line_tag(line)}" / "src"
        if not src.is_dir():
            return None
        try:
            findings, _msgs, _blocked = self.isolation_gate(src, ext_id)
        except Exception:  # noqa: BLE001 - 现扫失败不阻断启用
            return None
        return findings

    def enable_blocker(self, ext_id: str) -> str:
        """严格校验档下这个扩展能不能启用？空串 = 可以。

        判定只用**安装时存下的**扫描结果（启用时源码可能已被清理）。
        老版本装的扩展没有存档结果时，源码还在就现扫；都不行则放行 ——
        「扫不了就拒启用」会让玩家连停用都做不成。
        """
        mode = cfg_mod.extension_mode(self.cfg)
        if not cfg_mod.is_strict(mode):
            return ""
        exempt = [
            str(x) for x in ((self.cfg.get("extensions") or {}).get("strict_exempt") or [])
        ]
        if ext_id in exempt:
            return ""
        item = self.get(ext_id) or {}
        findings = self._stored_findings(item)
        if findings is None:
            findings = self._rescan_isolation(item) or []
        blocks = [f for f in findings if f.get("severity") == "block"]
        if not blocks:
            return ""
        head = "；".join(
            f"{b.get('code', '?')} {b.get('detail', '')}".strip() for b in blocks[:3]
        )
        more = f"；另有 {len(blocks) - 3} 条" if len(blocks) > 3 else ""
        return (
            f"严格校验模式禁止启用「{ext_id}」：{len(blocks)} 条跨扩展访问违规。\n\n"
            f"{head}{more}\n\n"
            "处理办法：① 让作者补 peer_access 声明；② 把模式调成「宽松校验」先观察；"
            "③ 在设置的严格校验豁免名单里加上这个扩展 id。"
        )

    def set_enabled(self, ext_id: str, enabled: bool, line: str | None = None) -> bool:
        """启用/停用（停用只是不参与启动组装，文件保留）。

        **按线生效**：同一个扩展可以同时装在 4.6 与 4.7，两条线各自有独立的
        覆盖层、独立的补丁结果，启用状态也必须独立。``line`` 给定时只改那条线
        的 ``lines[line].enabled``；不给时才退回旧的全局语义（CLI / 老数据）。

        顶层的 ``enabled`` 是镜像字段：只要还有任意一条线启用就是 ``True``。
        这样老代码读顶层不会误判「整包被停用」，界面也只需要看当前线那份。

        游戏运行期间会拒绝：运行中的实例已经把覆盖层 pck 挂进内存，
        此时改动启用列表会让「当前实例」与「下次组装」不一致，玩家看到的
        现象是「明明关掉了却还在生效」。判定依据是系统进程表而非界面状态
        （详见 :mod:`app.core.runtime_lock`）。

        ``strict`` 档下启用违规扩展会被拒（``LauncherError``）；
        停用永远放行 —— 否则玩家连「关掉问题扩展」都做不到。
        """
        from .. import runtime_lock

        runtime_lock.ensure_modifiable("启用/停用扩展")
        if enabled:
            blocker = self.enable_blocker(ext_id)
            if blocker:
                raise LauncherError(blocker)
        items = self.installed()
        hit = False
        for item in items:
            if item.get("id") != ext_id:
                continue
            lines = item.get("lines")
            if line and isinstance(lines, dict) and isinstance(lines.get(line), dict):
                lines[line]["enabled"] = bool(enabled)
                item["enabled"] = any(
                    bool(v.get("enabled", True)) for v in lines.values() if isinstance(v, dict)
                )
                hit = True
                continue
            if line and isinstance(lines, dict) and lines:
                # 这条线上没装：不改任何东西。返回 False 让调用方能提示
                # 「不在此线」，而不是静默写一个不存在的状态。
                continue
            item["enabled"] = bool(enabled)
            if isinstance(lines, dict):
                for value in lines.values():
                    if isinstance(value, dict):
                        value["enabled"] = bool(enabled)
            hit = True
        if hit:
            self._save_registry(items)
        return hit

    def mark_verified(self, ext_ids, line: str) -> int:
        """把这些扩展在该线的 ``verified`` 翻成 True（组合冒烟通过后调用）。

        为什么需要：合集包改成「整批一次冒烟」后，单包 ``install()`` 拿不到
        自己的冒烟结论（``verify_smoke=False``），注册表里的 ``verified``
        会是 False。整批真跑通了就必须补回来 —— 否则界面会一直显示
        「文件层已验证」这种半截状态，和实际不符。

        返回实际改动的条目数（幂等）。
        """
        wanted = {str(x) for x in (ext_ids or []) if str(x)}
        if not wanted:
            return 0
        items = self.installed()
        changed = 0
        for item in items:
            if str(item.get("id") or "") not in wanted:
                continue
            lines = item.get("lines")
            if isinstance(lines, dict) and isinstance(lines.get(line), dict):
                lines[line]["verified"] = True
            if str(item.get("engine_line") or "") == str(line):
                item["verified"] = True
            changed += 1
        if changed:
            self._save_registry(items)
        return changed

    def mark_launched(self, line: str, ext_ids, *, when: str | None = None) -> int:
        """启动成功后，把这些扩展在该线的「待插桩」翻成「已就绪」。

        调用方只应传**本次启动真实插桩成功**的 id（见
        ``enable.LaunchMods.ready_ids``）——由启动组装按「声明的钩子目标是否
        全部插桩」算出来。不能在这里凭 ``hooks_pending`` 自己推断：目标脚本
        缺失时插桩会被跳过，那种包必须保持待插桩，否则又是「界面说好了、
        进游戏没效果」。

        返回实际改动的条目数（幂等，重复调用返回 0）。
        """
        wanted = {str(x) for x in (ext_ids or []) if str(x)}
        if not wanted:
            return 0
        stamp = when or time.strftime("%Y-%m-%d %H:%M:%S")
        items = self.installed()
        changed = 0
        for item in items:
            if str(item.get("id") or "") not in wanted:
                continue
            lines = item.get("lines")
            if not isinstance(lines, dict):
                continue
            entry = lines.get(line)
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") not in (STATUS_NEEDS_HOOKS, STATUS_READY):
                continue
            if entry.get("status") == STATUS_READY and entry.get("hooks_verified_at"):
                continue
            entry["status"] = STATUS_READY
            entry["hooks_verified_at"] = stamp
            entry["hooks_verified_by"] = "launch"
            # 顶层是「最近一次安装那条线」的镜像，只有镜像指向本线时才同步，
            # 否则会把另一条线的状态盖掉。
            if str(item.get("engine_line") or "") == line:
                item["status"] = STATUS_READY
                item["hooks_verified_at"] = stamp
            changed += 1
        if changed:
            self._save_registry(items)
        return changed

    def uninstall(self, ext_id: str) -> bool:
        """卸载单个扩展（薄包装，保持老调用点的布尔返回值）。"""
        return self.uninstall_many([ext_id]).ok

    def uninstall_many(self, ids, *, check_lock: bool = True) -> "UninstallReport":
        """批量卸载：**一次**写注册表，然后逐个删目录。

        顺序不能反。先删目录再写注册表的话，写盘一旦失败就会留下
        「注册表里有、文件已没了」的坏条目——下次启动拿到不存在的
        overlay，用户点「卸载」也删不到东西。反过来，写盘失败时
        目录一个都没动，这就是「回滚」需要的全部。

        ``check_lock`` 只让 UI 在「已经提前确认过运行状态」时跳过重复检查；
        ``tasklist`` 一次要几百毫秒到十几秒，批量里逐个查是不可接受的。
        """
        wanted: list[str] = []
        for raw in ids or []:
            ext_id = str(raw or "").strip()
            if ext_id and ext_id not in wanted:
                wanted.append(ext_id)
        if not wanted:
            return UninstallReport(ok=False, message="没有选中任何扩展")

        if check_lock:
            from .. import runtime_lock

            runtime_lock.ensure_modifiable("卸载扩展")

        items = self.installed()
        existing = {str(i.get("id") or "") for i in items}
        targets = [i for i in wanted if i in existing]
        missing = [i for i in wanted if i not in existing]
        if not targets:
            return UninstallReport(
                ok=False, missing=missing, message="选中的扩展已不在注册表里"
            )

        target_set = set(targets)
        keep = [i for i in items if str(i.get("id") or "") not in target_set]
        # 唯一一次写盘。抛异常时目录还没动过，调用方拿到的就是「什么都没发生」。
        self._save_registry(keep)

        warnings: list[str] = []
        #: 卸载涉及的引擎线，用于随后作废这些线的插桩缓存
        touched_lines: set[str] = set()
        for ext_id in targets:
            entry = next((i for i in items if str(i.get("id") or "") == ext_id), {}) or {}
            lines = entry.get("lines")
            if isinstance(lines, dict):
                touched_lines.update(str(k) for k in lines if k)
            elif entry.get("engine_line"):
                touched_lines.add(str(entry["engine_line"]))
            target = paths.PACKS / ext_id
            if not target.exists():
                continue
            shutil.rmtree(target, ignore_errors=True)
            if target.exists():
                # 文件被占用/权限不足会留半个目录。它是孤儿文件，不再属于任何
                # 已安装条目；在这里如实报告，让用户去扩展目录手工清理。
                warnings.append(f"{ext_id} 的目录未能完全删除（可能被占用）：{target}")

        # 卸载后必须让派生缓存失效，否则会出现「卸载了、重启还在」：
        # 插桩层若还在，它里面就带着已删扩展的补丁与钩子派发。
        # 这是「卸载不彻底」的直接原因之一，不能只清 packs/ 就结束。
        cleared = 0
        try:
            from .enable import _purge_instrumented_cache

            cleared = sum(_purge_instrumented_cache(line) for line in sorted(touched_lines))
            if not touched_lines:
                cleared = _purge_instrumented_cache()
        except Exception as exc:  # noqa: BLE001 - 缓存清理失败不该让卸载失败
            warnings.append(f"插桩缓存未能清理：{exc}")

        # 安装中间目录（builds/_install_<id>_<line>）也一并清掉：
        # 里面留着这份扩展的 mods.json 与 mod_report.json，
        # 排障时极易被当成「它还在」的证据。
        for ext_id in targets:
            for stale in paths.BUILDS.glob(f"_install_{ext_id}_*"):
                shutil.rmtree(stale, ignore_errors=True)

        message = f"已卸载 {len(targets)} 个扩展"
        if cleared:
            message += f"；已清理插桩缓存 {cleared} 份"
        if missing:
            message += f"；{len(missing)} 个不在注册表里，已跳过"
        return UninstallReport(
            ok=True,
            removed=targets,
            missing=missing,
            warnings=warnings,
            message=message,
        )

    def uninstall_line(self, ids, line: str, *, delete_empty_package: bool = True) -> "UninstallReport":
        """只卸载这些扩展在 ``line`` 这条线上的安装。

        为什么不能直接用 :meth:`uninstall_many`：那个会删整个 ``packs/<id>``。
        老数据里同一个扩展可能在多条线各有一份覆盖层，卸载一条线不该顺手
        把别的那份也删掉（存量数据还要能安全清理）。
        ``lines`` 空了才删整包与注册表条目。
        """
        from .. import runtime_lock

        runtime_lock.ensure_modifiable("卸载该线扩展")

        wanted: list[str] = []
        for raw in ids or []:
            ext_id = str(raw or "").strip()
            if ext_id and ext_id not in wanted:
                wanted.append(ext_id)
        if not wanted:
            return UninstallReport(ok=False, message="没有选中任何扩展")

        items = self.installed()
        by_id = {str(i.get("id") or ""): i for i in items}
        targets = [i for i in wanted if i in by_id]
        missing = [i for i in wanted if i not in by_id]
        if not targets:
            return UninstallReport(ok=False, missing=missing, message="选中的扩展已不在注册表里")

        warnings: list[str] = []
        freed = 0
        removed_packages: list[str] = []
        keep: list[dict] = []
        touched = False

        for item in items:
            ext_id = str(item.get("id") or "")
            if ext_id not in set(targets):
                keep.append(item)
                continue
            lines = item.get("lines")
            if isinstance(lines, dict) and line in lines:
                lines.pop(line, None)
                touched = True
                target_dir = paths.PACKS / ext_id
                for name in (
                    f"overlay-{paths.line_tag(line)}.pck",
                    f"manifest-{paths.line_tag(line)}.v2.json",
                ):
                    path = target_dir / name
                    try:
                        if path.is_file():
                            freed += path.stat().st_size
                            path.unlink()
                    except OSError as exc:
                        warnings.append(f"{name}：{exc}")
                if delete_empty_package and not lines:
                    removed_packages.append(ext_id)
                    continue
                remain = [v for v in lines.values() if isinstance(v, dict)]
                if remain:
                    from .migrate import _mirror_from

                    newest = max(remain, key=lambda v: str(v.get("installed_at") or ""))
                    item.update(_mirror_from(newest))
                keep.append(item)
                continue
            if not isinstance(lines, dict) or not lines:
                # 旧格式：只有顶层一条线
                if str(item.get("engine_line") or "") == line:
                    removed_packages.append(ext_id)
                    continue
            keep.append(item)

        if not touched and not removed_packages:
            return UninstallReport(
                ok=False,
                missing=missing,
                message=f"这些扩展没有装到 {line} 线",
            )

        try:
            self._save_registry(keep)
        except OSError as exc:
            return UninstallReport(ok=False, missing=missing, message=f"注册表写入失败：{exc}")

        for ext_id in removed_packages:
            target_dir = paths.PACKS / ext_id
            if not target_dir.exists():
                continue
            shutil.rmtree(target_dir, ignore_errors=True)
            if target_dir.exists():
                warnings.append(f"{ext_id} 的目录未能完全删除（可能被占用）：{target_dir}")
            for stale in paths.BUILDS.glob(f"_install_{ext_id}_*"):
                shutil.rmtree(stale, ignore_errors=True)

        cleared = 0
        try:
            from .enable import _purge_instrumented_cache

            cleared = _purge_instrumented_cache(line)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"插桩缓存未能清理：{exc}")

        message = f"已卸载 {len(targets)} 个扩展在 {line} 线的安装"
        if removed_packages:
            message += f"（其中 {len(removed_packages)} 个已装不下其它线，整包删除）"
        if cleared:
            message += f"；已清理插桩缓存 {cleared} 份"
        if freed:
            message += f"；释放 {freed / 1024 / 1024:.1f} MB"
        if missing:
            message += f"；{len(missing)} 个不在注册表里，已跳过"
        return UninstallReport(
            ok=True,
            removed=targets,
            missing=missing,
            warnings=warnings,
            message=message,
        )

    def dependents_of(self, ids) -> list[dict]:
        """反查「谁依赖了这批要卸载的包」。

        现网 19 个包的 ``requires`` 全为空，所以这个功能目前是空跑；
        但作者一旦开始用依赖，卸掉被依赖方会让启动直接被
        ``requires.missing`` 拒绝——那时用户只会看到「无法启动」，
        根本联想不到是刚才卸载造成的。
        """
        wanted = {str(i).strip() for i in ids or [] if str(i).strip()}
        if not wanted:
            return []
        from . import enable as enable_mod

        out: list[dict] = []
        for item in self.installed():
            ext_id = str(item.get("id") or "")
            if not ext_id or ext_id in wanted:
                continue
            lines = item.get("lines") if isinstance(item.get("lines"), dict) else {}
            line_keys = list(lines) or [str(item.get("engine_line") or "")]
            hit_lines: list[str] = []
            hit_requires: set[str] = set()
            for line in line_keys:
                data = enable_mod.manifest_of(item, line)
                if not isinstance(data, dict):
                    continue
                for req in data.get("requires") or []:
                    rid = str(req.get("id") if isinstance(req, dict) else req)
                    if rid in wanted:
                        hit_requires.add(rid)
                        if line not in hit_lines:
                            hit_lines.append(line)
            if hit_requires:
                out.append(
                    {
                        "id": ext_id,
                        "name": str(item.get("name") or ext_id),
                        "line": " / ".join(sorted(hit_lines)),
                        "requires": sorted(hit_requires),
                    }
                )
        return out

    def preview_install(self, package: str | Path, *, target_line: str | None = None) -> "PreviewResult":
        """只读预览：解清单 + 与当前启用组合比对，**不写盘、不建覆盖层**。

        为什么要有它：单个 ``install`` 只做「这一个包自己能不能装」的静态
        校验，回答不了「装进去之后和已装的 10 个冲不冲」。用户在拖入时
        就该知道结果，而不是装完启动才发现被拒绝。
        """
        pkg = Path(package)
        if not pkg.exists():
            return PreviewResult(ok=False, message=f"文件不存在：{pkg}")
        try:
            probe = ext_manifest.load(pkg)
        except LauncherError as exc:
            return PreviewResult(ok=False, message=f"清单解析失败：{exc.message}")
        except Exception as exc:  # noqa: BLE001
            return PreviewResult(ok=False, message=f"清单解析失败：{exc}")

        line = target_line or probe.engine_line or "4.6"
        issues = [i.as_dict() for i in probe.issues]
        hard = [i for i in probe.issues if i.level == "error"]
        base = PreviewResult(
            ok=not hard,
            ext_id=probe.id,
            name=str(probe.data.get("name") or probe.id),
            version=str(probe.data.get("version") or ""),
            engine_line=line,
            issues=issues,
        )
        if hard:
            base.message = "静态校验未通过：" + "；".join(i.message for i in hard[:3])
            return base

        from . import enable as enable_mod
        from . import resolver as resolver_mod

        pairs = [
            (pid, data)
            for pid, data in enable_mod.enabled_manifests(self.cfg, line)
            if pid != probe.id  # 重装同名包不算和自己冲突
        ]
        pairs.append((probe.id, probe.data))
        resolution = resolver_mod.resolve(
            pairs,
            compose=cfg_mod.extension_compose(self.cfg),
            mode=cfg_mod.extension_mode(self.cfg),
            line=line,
            store=decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json"),
        )
        # 只报「新包参与」的问题：已有包互相之间的老问题不该算在这一次导入头上
        conflicts = [
            p.as_dict() for p in resolution.problems if probe.id in (p.ids or [])
        ]
        base.conflicts = conflicts
        errors = [c for c in conflicts if c.get("level") == "error"]
        warns = [c for c in conflicts if c.get("level") == "warn"]
        base.ok = not errors
        if errors:
            base.message = "与已启用扩展存在硬冲突：" + "；".join(
                str(c.get("message") or "") for c in errors[:3]
            )
        elif warns:
            base.message = f"未发现硬冲突，但有 {len(warns)} 处覆盖提醒"
        else:
            base.message = f"未发现冲突（已比对 {len(pairs) - 1} 个已启用扩展）"

        # 开发者校验专有：真合成一遍，看有没有包会「规则上合法、实际命中 0 次」。
        #
        # 为什么光靠 rules 层不够：处置记录（decisions.json）只决定「重叠归谁」，
        # 它不知道 replace 钩子会把原函数体改名搬走。等真正叠加时，后来者拿
        # 「函数签名 + 函数体开头」当锚点就必然命中 0 次，补丁整条丢失。
        # 2026-11-05 主城灰屏就是这条路径漏掉的，所以这里必须实跑。
        if not errors and cfg_mod.is_strict(cfg_mod.extension_mode(self.cfg)):
            failures = self._probe_anchor_failures(pairs, line)
            base.anchor_failures = failures
            if failures:
                base.ok = False
                names = sorted({str(f.get("ext_id") or "") for f in failures})
                base.message = (
                    f"开发者校验：{len(failures)} 处补丁在合成时命中 0 处"
                    f"（{'、'.join(names)}）。这些改动不会生效；"
                    "若改的是被继承的父类脚本，进主城会灰屏。"
                    "请改用 method_hooks，或与对方包协调锚点。"
                )
        return base

    def _probe_anchor_failures(
        self, pairs: list[tuple[str, dict]], line: str
    ) -> list[dict]:
        """实跑一遍合成层，取出「锚点命中 0 处」的补丁清单。

        只做计算，不写盘。失败一律吞掉——诊断辅助不该让导入本身报错。
        """
        from . import compose as compose_mod
        from .basecontent import BaseContent
        from .. import paths as paths_mod

        try:
            with tempfile.TemporaryDirectory(
                prefix="jjxf-probe-", dir=str(paths_mod.temp_dir())
            ) as tmp:
                base_content = BaseContent(self.cfg, line, include_installed=False)
                result = compose_mod.build(
                    pairs,
                    read_base=base_content.read,
                    read_overlay=lambda _pid, _target: None,
                    out_pck=Path(tmp) / "probe.pck",
                    line=line,
                )
        except Exception:  # noqa: BLE001 - 诊断失败退回「没发现问题」
            return []

        out: list[dict] = []
        for item in result.conflicts:
            if str(item.get("code") or "") != "compose.anchor":
                continue
            out.append(
                {
                    "ext_id": item.get("ext_id")
                    or ((item.get("ids") or [""])[0]),
                    "target": item.get("target"),
                    "anchor": item.get("anchor"),
                    "reason": item.get("reason"),
                }
            )
        return out

    # ------------------------------------------------------------ 安装

    def install(
        self,
        package: str | Path,
        progress: Progress | None = None,
        *,
        verify_smoke: bool = True,
        timeout: float = 240.0,
        target_line: str | None = None,
        allow_partial: bool = False,
        source_ref: dict | None = None,
    ) -> InstallResult:
        """安装一个扩展包。

        ``target_line`` 强制指定引擎线（默认按清单里的 ``baseline`` 推断）。
        同一个扩展可以装到多条线：每条线有独立的覆盖层
        （``packs/<id>/overlay-<line>.pck``），因为补丁是基于该线的原版
        脚本算出来的，换线必须重算。

        ``allow_partial=True`` 时，补丁只命中一部分也算成功（会记一条
        警告）。默认严格：补丁没命中就是失败——扩展依赖那个补丁才能工作，
        装上了但补丁没生效更糟。
        """
        pkg = Path(package)
        say = progress or (lambda _p, _l: None)
        lax = _loose_mode(self.cfg)
        # 钩子信息只在这些场合展示：
        # - 完全校验：作者/玩家都需要知道哪些钩子没落到源码上；
        # - 宽松校验 + 合集包：单个包导入不打扰，整合包才汇总提示。
        mode = cfg_mod.normalize_mode(cfg_mod.extension_mode(self.cfg))
        in_bundle = bool((source_ref or {}).get("bundle"))
        show_hook_info = _show_hook_info(mode, in_bundle)

        # source_ref 记录「这个扩展的原始包在哪」。合集安装时成员是从
        # 合集包里临时解出来的，那个临时目录用完就删——所以额外记下
        # 「合集包路径 + 成员名」，以后还能把它找回来（比如装到另一条线）。
        if source_ref:
            source_ref = dict(source_ref)
        else:
            source_ref = {"package": str(pkg), "member": "", "bundle": ""}

        # 部分兼容（跨版本）安装必须冒烟：被跳过的补丁可能正好是别的补丁
        # 依赖的来源，结果是脚本解析失败、整个 Autoload 加载不了。
        # 这种情况只有真跑一次才看得出来。
        if allow_partial and not verify_smoke and not lax:
            verify_smoke = True
            say(70, "跨版本安装：强制挂载冒烟（跳过补丁可能造成脚本解析失败）")
        # 宽松档：不额外强制挂载冒烟（那一档的承诺就是「装得上」，
        # 多跑一次 20 秒的冒烟等于把「宽松」变成「更慢」）。
        if lax:
            verify_smoke = False

        def fail(
            message: str,
            result: InstallResult | None = None,
            *,
            code: str = "install.unknown",
            detail: str = "",
        ) -> InstallResult:
            out = result or InstallResult(ok=False)
            out.ok = False
            out.message = message
            out.code = code
            out.detail = detail or msg_mod.safe_detail(message)
            say(100, message)
            return out

        if not pkg.exists():
            return fail("找不到这个扩展包。", code="install.not_found", detail=str(pkg))

        # --- 1 读取清单
        say(4, "读取扩展清单")
        try:
            probe = ext_manifest.load(pkg)
        except LauncherError as exc:
            return fail(msg_mod.human("install.manifest", hint=True), code="install.manifest", detail=str(exc.message))
        except Exception as exc:  # noqa: BLE001
            return fail(msg_mod.human("install.manifest", hint=True), code="install.manifest", detail=str(exc))

        ext_id = probe.id
        declared_raw = probe.engine_line
        declared_line = declared_raw or "4.6"
        # target_line 优先：用户明确说要装到哪条线，就按哪条线算补丁与覆盖层
        line = target_line or declared_line
        baseline_id = (probe.data.get("baseline") or {}).get("id") or "jjxf.v1.45"
        if target_line and target_line != declared_line:
            # 一条线只装自己匹配的扩展。跨线重算补丁看似能跑，实际会让
            # 「补丁命中一部分」的包静默失效——用户明确要求硬拒绝。
            declared_note = (
                f"清单声明 {declared_line}"
                if declared_raw
                else "清单未声明引擎线（按 4.6 处理）"
            )
            message = (
                f"「{getattr(probe, 'name', '') or ext_id}」只适配 {declared_line}，"
                f"不能装到 {target_line}。\n"
                f"{declared_note}；补丁是按目标线原版脚本算的，跨线重装会让功能静默失效。\n"
                f"请等作者发布 {target_line} 版，或把游戏切到 {declared_line} 线后再装。"
            )
            return fail(
                message,
                InstallResult(
                    ok=False,
                    ext_id=ext_id,
                    name=getattr(probe, "name", "") or "",
                    engine_line=declared_line,
                    line_mismatch=True,
                    declared_line=declared_line,
                ),
            )

        # --- 2 静态校验（带基线索引 + 源码根）
        say(14, "静态校验：文件完整性与钩子核对")
        source_root = None
        src_meta = (self.cfg.get("runtime_sources") or {}).get(line)
        if src_meta and src_meta.get("project_godot"):
            candidate = Path(src_meta["project_godot"]).parent
            if candidate.exists():
                source_root = candidate
        if source_root is None:
            # 设置页只填「反解工程」时 runtime_sources 可能还是空的；两处指的
            # 是同一份源码，回退一次——否则补丁能读源码、钩子却仍判缺失。
            reversed_root = (self.cfg.get("sources") or {}).get("reversed_project") or ""
            if reversed_root and Path(reversed_root).is_dir():
                source_root = Path(reversed_root)
        try:
            from ..baseline import path_set

            baseline_paths = path_set(line)
        except Exception:  # noqa: BLE001
            baseline_paths = None

        # --- 3.1 清单静态校验：**两档都跑完整一套**
        #
        # 这条是设计的核心，不是可以省的步骤。宽松档的承诺是「查出来 +
        # 告诉玩家 + 照装」，不是「不查」—— 不查就没得告诉，玩家看到的
        # 就只剩黑屏。所以两档都走 ext_manifest.load()，差别只在处置：
        #
        #   | 问题级别 | 宽松档 | 完全档 |
        #   |---|---|---|
        #   | _LAUNCHER_FATAL（装上也跑不起来） | 拒装 | 拒装 |
        #   | 其它 error（补丁锚点失配等） | 记下来，照装 | 拒装 |
        #   | warn / info | 记录 | 记录 |
        #
        # 旧的三档制里 normal 档是「拿 probe 的浅层结果直接过」，等于把
        # 补丁命中、钩子目标、文件清单全跳了。那个档已删，这条路也封掉。
        try:
            m = ext_manifest.load(pkg, source_root, baseline_paths)
        except Exception as exc:  # noqa: BLE001
            return fail(msg_mod.human("install.verify"), code="install.verify", detail=str(exc))

        issues = [i.as_dict() for i in m.issues]
        fatal = [i for i in m.issues if i.level == "error" and i.code in _LAUNCHER_FATAL]
        if fatal:
            # 「装上也必然跑不起来」与档位无关 —— 硬拦。
            return fail(
                "这个扩展不适配当前启动器，已跳过安装。",
                InstallResult(ok=False, ext_id=ext_id, issues=issues),
                code="install.incompatible",
                detail="；".join(i.message for i in fatal[:3]),
            )
        hard = [i for i in m.issues if i.level == "error"]
        if hard and not lax:
            # 完全档：任何 error 级清单问题都不放行
            return fail(
                msg_mod.human("install.verify"),
                InstallResult(ok=False, ext_id=ext_id, issues=issues),
                code="install.verify",
                detail="；".join(i.message for i in hard[:3]),
            )

        # --- 3.2 源码基底闸门（**两档都硬拦**）
        #
        # text_patches 的锚点、method_hooks 的插桩都必须落在源码文本上。
        # 这条线拿不到可信源码时，继续装只有两种结果：补丁静默不生效，
        # 或者锚点命中在错误的位置。两种都比「装不上」更难查，所以宁可不装。
        #
        # 注意闸门看的是「本机这条线有没有可信源码」，不是包里写没写声明：
        # 声明只是溯源，能不能打补丁取决于本机实际能读到的原文。
        if m.needs_source:
            from . import basecontent

            ok_src, why = basecontent.source_ready(self.cfg, line)
            if not ok_src:
                return fail(
                    f"这条线缺少可用的反解源码，已拒绝安装「{getattr(probe, 'name', '') or ext_id}」。"
                    f"\n\n{why}",
                    InstallResult(
                        ok=False,
                        ext_id=ext_id,
                        name=str(probe.data.get("name") or ext_id),
                        engine_line=line,
                        issues=issues,
                    ),
                    code="install.no_source",
                    detail=why,
                )

        # --- 3 解包
        say(26, "解包载荷")
        work = paths.BUILDS / f"_install_{ext_id}_{paths.line_tag(line)}"
        try:
            ext_apply.extract_payload(pkg, work / "src")
        except zipfile.BadZipFile as exc:
            # 真损坏：CRC/中央目录都读不了，别让下面的兜底把它说成路径问题
            return fail(msg_mod.human("install.corrupt"), code="install.corrupt", detail=str(exc))
        except (ArchivePathTooLongError, OSError) as exc:
            if is_longpath_error(exc):
                return fail(
                    msg_mod.human("install.longpath", hint=True),
                    code="install.longpath",
                    detail=str(exc),
                )
            return fail(msg_mod.human("install.unpack"), code="install.unpack", detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return fail(msg_mod.human("install.unpack"), code="install.unpack", detail=str(exc))

        # --- 3.4 联机模块闸门（协议层）
        #
        # 1.5 只做单机启动，不开放 P2P / 直连协议。这里在解包后、生成覆盖层
        # 前拦一道：被拒的包不会消耗补丁计算时间，也不会在 packs/ 留下孤儿。
        # 只看扩展包本身——自定义版本基线自带的联机能力不归这里管。
        say(30, "联机模块检查")
        try:
            net_report = netguard.scan_package(work / "src", m.data)
        except Exception as exc:  # noqa: BLE001
            # 扫描器自身出错不该阻断安装，但必须留痕，便于事后追查。
            net_report = netguard.NetReport()
            net_report.hits.append(
                netguard.NetHit(
                    rule="scanner_error",
                    reason=f"联机扫描未完成（已放行）：{exc}",
                    where=str(pkg),
                    level="warn",
                )
            )
        allow_netplay = cfg_mod.netplay_open(self.cfg)
        if net_report.blocked and not allow_netplay:
            return fail(
                msg_mod.human("install.netguard"),
                InstallResult(
                    ok=False,
                    ext_id=ext_id,
                    name=m.data.get("name", ext_id),
                    engine_line=line,
                    message=net_report.message(),
                    issues=issues + [h.as_dict() for h in net_report.blocks],
                ),
            )
        net_warnings = [h.as_dict() for h in net_report.warnings]
        if allow_netplay and net_report.blocked:
            # 联机协议已开放：阻断项降级为提示，保留命中详情便于排障。
            for hit in net_report.blocks:
                rec = hit.as_dict()
                rec["level"] = "warn"
                rec["reason"] = "联机协议已开放：" + str(rec.get("reason") or "")
                net_warnings.append(rec)

        # --- 3.4 完全校验：导入即准入
        #
        # 语义不是「先装进去、启动前再拦」，而是**导入时就把话说死**：
        # 待装包与当前已启用组合做一次完整解析，只要还有未处置的位置冲突，
        # 就不写 overlay、不进注册表，直接拒绝。玩家因此不会遇到
        # 「装上了但进游戏才炸」。
        if cfg_mod.is_strict(mode):
            preview = self.preview_install(pkg, target_line=line)
            hard = [c for c in preview.conflicts if c.get("level") == "error"]
            if hard:
                detail = "；".join(str(c.get("message") or "") for c in hard[:4])
                return fail(
                    "开发者校验：与已启用扩展存在未处置冲突，已拒绝安装。\n" + detail,
                    InstallResult(
                        ok=False,
                        ext_id=ext_id,
                        name=str(probe.data.get("name") or ext_id),
                        engine_line=line,
                        conflicts=hard,
                    ),
                    code="install.conflict",
                    detail=detail,
                )

        # --- 3.5 扩展隔离校验（三档：不扫 / 只记 / 拒装）
        #
        # 放在「生成覆盖层」之前是刻意的：被拒的包不该再消耗时间去做补丁，
        # 也不该在 packs/ 下留下一个孤儿覆盖层。
        say(34, "扩展隔离校验")
        # **两档都扫**。宽松档也要扫，这条是设计要点不是疏忽：
        #   1) 宽松档的承诺是「查出来 + 告诉玩家 + 放行」，不查就没得告诉；
        #   2) 扫描结果要存进注册表，玩家之后切到完全校验时，启用闸门
        #      才有判据（否则「装的时候宽松、切严格后照样能启用」＝形同虚设）。
        # 旧的三档制里 normal 档会整段跳过（零开销），那个档已经删了。
        try:
            iso_findings, iso_msgs, iso_blocked = self.isolation_gate(
                work / "src", ext_id, m.data
            )
        except Exception as exc:  # noqa: BLE001
            # 扫描器自身出错不该阻断安装（它只是辅助判据），但要留痕
            iso_findings, iso_msgs, iso_blocked = (
                [],
                [f"隔离扫描失败（已放行）：{exc}"],
                False,
            )
        if iso_blocked:
            return fail(
                msg_mod.human("install.isolation"),
                InstallResult(
                    ok=False,
                    ext_id=ext_id,
                    name=m.data.get("name", ext_id),
                    engine_line=line,
                    isolation=iso_findings,
                    isolation_blocked=True,
                    message="；".join(iso_msgs),
                ),
            )

        # --- 4 生成覆盖层
        say(45, "生成覆盖层 pck")
        target_dir = paths.PACKS / ext_id
        dir_existed = target_dir.exists()
        target_dir.mkdir(parents=True, exist_ok=True)
        # 每条线一个覆盖层：补丁是基于该线的原版脚本算出来的，换线必须重算
        overlay_path = target_dir / f"overlay-{paths.line_tag(line)}.pck"
        try:
            base_reader = BaseContent(self.cfg, line, exclude=ext_id)
            overlay = ext_apply.build_overlay(
                pkg,
                m,
                overlay_path,
                baseline_paths,
                line,
                work_dir=work,
                read_base=base_reader.read,
                allow_partial=allow_partial,
            )
        except Exception as exc:  # noqa: BLE001
            # 本次新建的空目录要收回：否则失败一次就在 packs/ 下留一个
            # 空壳，界面不显示、用户也删不掉，只会越积越多。
            if not dir_existed:
                shutil.rmtree(target_dir, ignore_errors=True)

            # -------------------------------------------------------
            # **4.7 分级：锚点失配不再整包拒装**（2026-11-08 决策）
            #
            # 4.7 强制 strict（剧情会更新，必须严格判定）。但「严格判定」
            # 和「严格拒装」是两件事 —— 一个包可能只有 3 条补丁，其中
            # 1 条因为剧情修复版多了一行注释而失配，另 2 条仍然有效。
            # 整包拒装等于让玩家什么都得不到。
            #
            # 现在的处理：能装多少装多少，装不上的那几条明确记下来。
            # 前提是「至少有一部分能用」—— 全失配还是要拒装，那说明
            # 这个包整体不适配，装上去只是个空壳。
            #
            # 判定依据是 build_overlay 抛出的异常的上下文；拿不到明细时
            # 保守退回原行为（拒装），不能因为判不出来就放行。
            # -------------------------------------------------------
            partial_ok = _partial_can_install(exc, line, allow_partial)
            if partial_ok:
                # 注意：不能走 ``fail()`` —— 那个闭包会把 ``ok`` 强制改回
                # False（它叫 fail 就是因为这个）。部分安装是**成功**，
                # 只是带一条「有些内容没生效」的说明。
                say(100, "部分内容未生效，其余已安装")
                out = InstallResult(
                    ok=True,
                    ext_id=ext_id,
                    name=m.data.get("name", ext_id),
                    engine_line=line,
                    baseline=baseline_id,
                )
                out.message = f"已安装（部分内容与当前 {line} 版对不上）"
                out.code = "install.partial"
                out.detail = (
                    f"{exc}；建议让作者按 {line} 线实际源码重写锚点"
                )
                return out

            if not cfg_mod.is_strict(mode):
                # 宽松档不该看到「补丁未命中」这类实现层报错。对玩家来说，
                # 结论只有一个：这个包和当前启动器/游戏版本不匹配。
                return fail(
                    "这个扩展不适配当前启动器，已跳过安装。",
                    code="install.incompatible",
                    detail=str(exc),
                )
            return fail(msg_mod.human("install.overlay", hint=True), code="install.overlay", detail=str(exc))

        # --- 4.5 补丁落地统计：跳过的必须说出来
        #
        # ``allow_partial`` 下取不到原文、或命中数不符的补丁会被**逐条跳过**。
        # 过去最终文案只说「已安装；与已装扩展没有冲突」，玩家以为功能都在，
        # 实际核心补丁一条没打上（2026-11-13 实测：0/12 的包也显示「已安装」）。
        # 这里把没落地的补丁数记进 issues 并附进最终文案。
        skipped_patches = [
            p
            for p in (overlay.patches or [])
            if str(p.get("kind") or "") in {"text", "data", "skip"}
            and int(p.get("applied") or 0) == 0
            and p.get("issues")
        ]
        if skipped_patches:
            targets: list[str] = []
            for p in skipped_patches:
                t = str(p.get("target") or "")
                if t and t not in targets:
                    targets.append(t)
            issues.append(
                {
                    "level": "warn",
                    "code": "patch.skipped",
                    "message": (
                        f"{len(skipped_patches)} 处补丁未应用（目标 {line} 版里对不上）："
                        + "、".join(targets[:3])
                        + ("…" if len(targets) > 3 else "")
                    ),
                }
            )

        # --- 5 回读校验
        say(64, "回读校验覆盖层")
        try:
            reader = PckReader.open(overlay_path)
            if len(reader) != overlay.entry_count:
                raise ValueError(f"条目数不符：{len(reader)} != {overlay.entry_count}")
        except Exception as exc:  # noqa: BLE001
            return fail(msg_mod.human("install.pck"), code="install.pck", detail=str(exc))

        result = InstallResult(
            ok=True,
            ext_id=ext_id,
            name=m.data.get("name", ext_id),
            version=m.data.get("version", ""),
            engine_line=line,
            baseline=baseline_id,
            status=STATUS_READY,
            overlay=overlay_path,
            entry_count=overlay.entry_count,
            sha256=overlay.sha256,
            issues=issues,
            isolation=iso_findings,
        )

        # --- 6 挂载冒烟（真挂起来跑一次 headless，并把 mod 入口真装一遍）
        if verify_smoke:
            say(78, "运行校验：挂载覆盖层 + 装载 mod 入口")
            try:
                ok, detail = self._smoke(
                    line,
                    baseline_id,
                    overlay_path,
                    timeout=timeout,
                    ext_id=ext_id,
                    manifest_data=m.data,
                    source_root=source_root,
                )
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"冒烟异常：{exc}"
            result.verified = ok
            if not ok:
                result.ok = False
                result.code = "install.smoke"
                result.detail = str(detail)
                result.message = msg_mod.human("install.smoke", hint=True)
                say(100, result.message)
                # 校验没过就别留覆盖层：它没进注册表，是孤儿文件，
                # 留着只会让人以为装上了。删掉更干净。
                try:
                    if overlay_path.exists():
                        overlay_path.unlink()
                except OSError:
                    pass
                return result
            # 探针声明了钩子，但一个都没注册上 —— 这就是过去「装上没反应」
            # 的静默形态：ModHost 照样报装载成功，界面一片绿，钩子全是死代码。
            # 单独出一条 warn，让它出现在安装结果的 problem 列表里。
            wanted = int(getattr(self, "_probe_hook_count", 0) or 0)
            got = int(getattr(self._last_probe, "mods_hooks", 0) or 0)
            if wanted and not got:
                issues.append(
                    {
                        "level": "warn",
                        "code": "hook.not_registered",
                        "message": (
                            f"插桩点已声明 {wanted} 个，但扩展一个都没注册上；"
                            f"它的功能大概率不生效（多为钩子目标与当前源码对不上）"
                        ),
                    }
                )
        else:
            result.verified = False

        # --- 落盘注册
        say(94, "写入注册表")
        # 钩子清单仍要扫：启动时插桩靠它。玩家版只是**不核对**钩子能不能
        # 落到源码上（那是作者/开发者才需要看的结论），不是不插桩。
        runtime_hooks = _scan_runtime_hooks(work / "src")
        missing_scripts = []
        if show_hook_info:
            # 钩子能否落地，取决于反解源码里有没有对应脚本（插桩要在源码上做）。
            # 普通模式与「宽松校验的单包导入」都不做这一步：玩家看不到钩子，
            # 也就不该因此把扩展标成「待插桩」。
            for h in runtime_hooks:
                if source_root is None or not (source_root / str(h["script"])).exists():
                    missing_scripts.append(str(h["script"]))
        if missing_scripts:
            result.status = STATUS_NEEDS_HOOKS
        try:
            digest = sha256_file(overlay_path)
            (target_dir / f"manifest-{paths.line_tag(line)}.v2.json").write_text(
                json.dumps(m.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            line_entry = {
                "line": line,
                "baseline": baseline_id,
                "status": result.status,
                "overlay": cfg_mod.stored_path(overlay_path),
                "entry_count": result.entry_count,
                "sha256": digest,
                "verified": result.verified,
                # **按线启用**：装到 4.7 不能顺手把 4.6 那份也重新启用。
                "enabled": True,
                "runtime_hooks": runtime_hooks,
                "patches": overlay.patches,
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "hooks_pending": len(m.method_hooks),
                # 作者声明使用的反解源码（name / sha256 / line）。留档用于
                # 事后核对「这个包的补丁是按哪份原文算的」。
                "sources": m.sources,
                # 隔离扫描结果随注册表一起存：启用闸门要读它，
                # 而那时源码可能已经被清理（作者发的是纯 pck 包）。
                "isolation": iso_findings,
            }

            # 同一个扩展可以装到多条线：顶层字段保留「最近一次安装的那条线」
            # （兼容旧数据与既有界面），完整的多线信息放在 lines 里。
            items = [i for i in self.installed() if i.get("id") != ext_id]
            previous = next((i for i in self.installed() if i.get("id") == ext_id), None)
            lines: dict[str, dict] = {}
            if previous:
                old_lines = previous.get("lines")
                if isinstance(old_lines, dict):
                    lines = {k: dict(v) for k, v in old_lines.items() if isinstance(v, dict)}
                elif previous.get("engine_line"):
                    # 旧格式：只有一条线
                    lines[str(previous["engine_line"])] = {
                        "line": str(previous["engine_line"]),
                        "baseline": previous.get("baseline", ""),
                        "status": previous.get("status", ""),
                        "overlay": previous.get("overlay", ""),
                        "entry_count": previous.get("entry_count", 0),
                        "sha256": previous.get("sha256", ""),
                        "verified": previous.get("verified", False),
                        "runtime_hooks": previous.get("runtime_hooks") or [],
                        "patches": previous.get("patches") or [],
                        "installed_at": previous.get("installed_at", ""),
                        "hooks_pending": previous.get("hooks_pending", 0),
                        "enabled": previous.get("enabled", True),
                    }
            lines[line] = line_entry

            entry = {
                "id": ext_id,
                "name": result.name,
                "version": result.version,
                # 作者来自 manifest.v2 的 author 字段（规范里本来就有，只是过去
                # 解析后被丢掉了）。留空时界面显示「未标注」，不要伪造来源。
                # 注意取 m.data 而不是 m.author：Manifest 没有 author 属性，
                # 直接属性访问会抛 AttributeError 导致整个安装失败（踩过）。
                "author": str((m.data or {}).get("author") or "").strip(),
                "engine_line": line,
                "baseline": baseline_id,
                "status": result.status,
                "overlay": cfg_mod.stored_path(overlay_path),
                "entry_count": result.entry_count,
                "sha256": digest,
                "verified": result.verified,
                # 顶层是「任意一条线启用」的镜像。本次安装的那条线一定是启用
                # 的，但别把另一条线玩家手动关掉的记录冲掉——所以这里按 lines 算。
                "enabled": any(
                    bool(v.get("enabled", True)) for v in lines.values() if isinstance(v, dict)
                ),
                "runtime_hooks": runtime_hooks,
                "patches": overlay.patches,
                "source_package": str(pkg),
                "source_ref": source_ref,
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "hooks_pending": len(m.method_hooks),
                "lines": lines,
            }
            items.append(entry)
            self._save_registry(items)

            # 版本变化后主动作废本线的插桩缓存。
            #
            # 为什么不依赖缓存键自动失效：键里算的是「钩子规格 + 源文件内容 +
            # overlay 签名」，如果新版本恰好没动 method_hooks、也没让 overlay
            # 字节变化（例如只改了 mod 自己的运行期脚本），键就不变。这种情况
            # 复用缓存其实是正确的，但作者有时会修正钩子的隐含语义而不改声明，
            # 留着旧缓存就会得到「装了新版本、行为还是旧的」这种极难查的现象。
            # 主动清掉的代价只是多一次插桩（毫秒级），值得。
            old_version = str((previous or {}).get("version") or "")
            if previous and old_version and old_version != str(result.version):
                # 延迟导入：enable.py 在模块层导入了本模块，直接顶层 import 会循环
                from .enable import _purge_instrumented_cache

                cleared = _purge_instrumented_cache(line)
                result.message_extra = f"；已作废本线插桩缓存 {cleared} 份（版本 {old_version} → {result.version}）"
        except Exception as exc:  # noqa: BLE001
            return fail(msg_mod.human("install.register"), code="install.register", detail=str(exc))

        # --- 7 组合检查：装完立刻回答「它和已装的冲不冲」
        # 单个包的静态校验只证明「自己没问题」，证明不了「加进去还能共存」。
        # 这一步读一遍该线所有已启用包的清单做解析，毫秒级，失败也不影响安装结果。
        say(97, "组合检查：与已启用扩展比对")
        try:
            result.conflicts = _post_install_conflicts(self.cfg, {ext_id})
        except Exception:  # noqa: BLE001 - 组合检查只是提示，不该让安装失败
            result.conflicts = []

        # 联机扫描只出告警（协议层已放行），并入 issues 便于界面统一展示。
        if net_warnings:
            result.issues.extend(net_warnings)

        result.degraded = [
            c for c in result.conflicts if c.get("action") == "degraded"
        ]
        skipped_note = f"；{len(skipped_patches)} 处补丁未生效" if skipped_patches else ""
        if not cfg_mod.is_strict(mode):
            # 宽松档：只需要知道「装上了 / 有没有后遗症」，不需要实现细节
            result.message = f"{result.name} {result.version} 已安装"
            if missing_scripts:
                result.message += "；部分功能需要重启游戏后生效"
            result.message += skipped_note + _conflict_summary(result.conflicts)
        else:
            hook_text = ""
            if show_hook_info:
                hook_text = (
                    f"；{len(runtime_hooks)} 个钩子将在启动时插桩"
                    if runtime_hooks and not missing_scripts
                    else (
                        f"；{len(missing_scripts)} 个钩子找不到源码，无法插桩"
                        if missing_scripts
                        else ""
                    )
                )
            result.message = (
                f"{result.name} {result.version} 已安装"
                + ("（文件层已验证）" if result.verified else "")
                + hook_text
                + skipped_note
                + str(result.message_extra or "")
                + _conflict_summary(result.conflicts)
                + (f"；联机检查 {len(net_warnings)} 处提示" if net_warnings else "")
            )
        # ----------------------------------------------------------
        # 保留包内的验证记录（``verification.json``）到安装目录。
        #
        # 2026-11-10 修：此前安装只取载荷与清单，**把 verification.json 丢掉了**，
        # 而 ``verify_mark.judge()`` 正是从 ``packs/<id>/verification.json``
        # 读记录。于是 ``jjm_pack seal`` 好不容易生成的记录，一装就没了 ——
        # 4.7 严格档随即以 ``verify.absent`` 剔除该扩展。
        #
        # 这就是「seal 成功、装入成功、游戏里却没反应」的最后一环。
        #
        # 注意：不能重新算指纹写一份新的 —— 必须**原样搬运**作者签封时
        # 算出的那份。记录的 `code_digest` 绑的是作者出包时的 4.7 代码层，
        # 内容和安装端的当前代码层是两个概念；重算会把 stale 伪装成 verified。
        # ----------------------------------------------------------
        try:
            from . import verify_mark as _vm

            if pkg.is_file():
                with zipfile.ZipFile(pkg) as _zf:
                    if _vm.RECORD_NAME in _zf.namelist():
                        _rec_bytes = _zf.read(_vm.RECORD_NAME)
                        (target_dir / _vm.RECORD_NAME).write_bytes(_rec_bytes)
        except (OSError, zipfile.BadZipFile, KeyError):
            # 记录搬运失败不该让安装失败：包本身是好的，只是缺验证标志，
            # 后续 judge 会判 absent 并给出明确提示。
            pass

        say(100, result.message)
        return result

    # ------------------------------------------------------------ 冒烟

    def isolation_gate(
        self,
        src_root: str | Path,
        ext_id: str,
        manifest_data: dict | None = None,
        *,
        mode: str | None = None,
    ) -> tuple[list[dict], list[str], bool]:
        """按当前隔离模式扫一遍扩展源码，返回 ``(发现, 人话消息, 是否阻断)``。

        **两档都扫**，差别只在阻断：

        | 档位 | 扫描 | 记录 | 阻断 |
        |---|---|---|---|
        | ``loose`` | 扫 | 是 | 否（提示给玩家看） |
        | ``strict`` | 扫 | 是 | **有 block 级就阻断** |

        宽松档也扫是有意的：它要在**启动前**告诉玩家「你装的这些包里有
        跨扩展越界访问」，而不是等他看到黑屏才去猜。

        ``manifest_data`` 用来做 L0-11 双向核对（声明了 vs 实际做了）。
        没有源码（纯 pck 扩展）时无从扫描，直接放行 —— 不能因为
        「扫不了」就把人家拒了。
        """
        current = mode or cfg_mod.extension_mode(self.cfg)

        exempt = [
            str(x) for x in ((self.cfg.get("extensions") or {}).get("strict_exempt") or [])
        ]
        if ext_id in exempt:
            return [], [f"{ext_id} 在用户豁免名单里，跳过隔离校验"], False

        files = isolation_mod.load_src_files(Path(src_root))
        if not files:
            return [], [], False

        findings = isolation_mod.scan_tree(files, ext_id)
        declared = (manifest_data or {}).get("peer_access") or {}
        findings += isolation_mod.reconcile(declared, findings)

        dicts = [f.as_dict() for f in findings]
        blocks = [f for f in findings if f.severity == "block"]
        messages: list[str] = []
        if blocks:
            head = "；".join(f"{b.code} {b.detail}" for b in blocks[:3])
            if len(blocks) > 3:
                head += f"；另有 {len(blocks) - 3} 条"
            # 「阻断」的措辞要准：这些是真会互相影响（写同一文件 / 抢命名 /
            # 依赖成环），不是代码风格问题。
            messages.append(f"冲突 {len(blocks)} 条（会互相影响）：{head}")
        warns = [f for f in findings if f.severity != "block"]
        if warns:
            # 提示级不只是「不影响安装」，还要让作者知道该补什么声明。
            # 单独列出前几条，否则「另有 N 条」等于没说。
            codes = sorted({f.code for f in warns})
            messages.append(
                f"提示 {len(warns)} 条（不影响安装，建议补 peer_access 声明）："
                f"{'、'.join(codes)}"
            )

        blocked = bool(blocks) and cfg_mod.is_strict(current)
        return dicts, messages, blocked

    def _smoke(
        self,
        line: str,
        baseline_id: str,
        overlay: Path,
        timeout: float,
        *,
        ext_id: str = "",
        manifest_data: dict | None = None,
        source_root: Path | None = None,
    ) -> tuple[bool, str]:
        """装完立刻验证「覆盖层能挂 + 入口能装起来」。

        **改造要点**：必须把 mods.json 一起传进去。
        旧实现只挂覆盖层，``ModHost`` 根本没被喂清单，于是 mod 的 ``install()``
        一次都没执行过——"安装成功"只证明了 pck 能被 load_resource_pack，
        证明不了这个 mod 能跑。这是"装了却用不了"的根因。
        """
        from .. import smoke as smoke_mod

        self._probe_hook_count = 0
        mods_json = (
            self._write_probe_mods(line, ext_id, overlay, manifest_data, source_root)
            if ext_id
            else None
        )
        result = smoke_mod.run_overlay(
            line,
            overlay,
            baseline_id=baseline_id,
            timeout=timeout,
            mods_json=mods_json,
        )
        # 把 mod 装载明细挂到结果里，供调用方出可读提示
        self._last_probe = result
        return result.ok, result.summary()

    def _write_probe_mods(
        self,
        line: str,
        ext_id: str,
        overlay: Path,
        manifest_data: dict | None = None,
        source_root: Path | None = None,
    ) -> Path | None:
        """为安装期校验造一份最小 mods.json（只含这一个扩展）。

        为什么不直接用 build_for_launch：它会带上全部已启用扩展，
        一个坏包就会让别的包也过不了校验，定位不到是谁的问题。
        安装期只该回答"这一个包能不能装起来"。

        **钩子必须带上（2026-11-10 修）**。此前这里硬编码 ``"hooks": []``，
        于是插桩层一个点都没有；扩展的 ``install()`` 里
        ``api.add_method_hook(...)`` 对每个钩子都返回 false，而扩展普遍写成
        ``if not add_method_hook(...): return false`` —— 第一个钩子失败就整包
        装载失败。日志表现是::

            [Hook] 已声明插桩点 0 个
            [Hook] 插桩点不存在：scripts/game.gd::equipment_attributes (after)
            [ModHost] 扩展失败[mch.gem_forge]：属性汇总或背包使用接入失败。

        这条日志过去被读成「这个包的钩子坏了」，其实是**探针自己没带钩子**
        造出来的假阴性。装上真的钩子，探针才真的在回答「这个包能不能跑」。

        单包探针的边界：只插「本包能看见的脚本」——反解源码里有的，或包内
        自带脚本。指向 ``requires`` 声明的其它扩展的钩子不插（那份脚本此刻
        根本不存在，插了必然失败），把它留给真实启动时的合并插桩。
        """
        # 入口优先取**传进来的清单**。
        #
        # 不能只靠 ``_entry_of``：它读 ``packs/<id>/manifest-<line>.v2.json``，
        # 而那份文件是在冒烟**之后**的「落盘注册」才写的。首次安装时它还不存在，
        # 于是探针直接返回 None —— 不传 mods.json，ModHost 根本不会实例化这个
        # 扩展，冒烟只剩「覆盖层能挂」，等于又退回「装了却用不了」的老坑。
        # 2026-11-10 实测：沙箱里装 gem_forge 就踩到了，探针 0 个钩子、
        # 0 个 mod 装载，却报「文件层已验证」。
        entry = str(((manifest_data or {}).get("scripts") or {}).get("entry") or "")
        if not entry:
            entry = self._entry_of(ext_id, line)
        if not entry:
            # 数据/资源类包没有运行期入口，本来就不该被 ModHost 实例化。
            # 此时不传 mods.json，只验证覆盖层挂载，这是正确行为。
            return None

        rd = paths.BUILDS / f"_install_{ext_id}_{paths.line_tag(line)}"
        rd.mkdir(parents=True, exist_ok=True)
        hooks = _probe_hooks(rd, manifest_data, source_root)
        # 留给安装流程做「声明了 N 个、实际注册 0 个」的静默失效判定。
        self._probe_hook_count = len(hooks)
        payload = {
            "schema": 1,
            "line": line,
            "hooks": hooks,
            "mods": [{"id": ext_id, "entry": entry}],
        }
        target = rd / "mods.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # 清掉上一轮的装载报告，避免读到过期结果
        stale = rd / "mod_report.json"
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
        return target

    def _entry_of(self, ext_id: str, line: str) -> str:
        """从该线已安装的清单里取运行期入口路径。

        为什么要自己实现一份、不复用 ``enable._entry_of``：
        ``enable`` 模块在顶部 import 了本模块（要 ExtensionService），
        这里反向 import 会形成循环依赖。逻辑只有几行，就地实现更安全。
        """
        root = paths.PACKS / ext_id
        candidates = []
        if line:
            candidates.append(root / f"manifest-{paths.line_tag(line)}.v2.json")
        candidates.append(root / "manifest.v2.json")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entry = ((data.get("scripts") or {}).get("entry") or "") if isinstance(data, dict) else ""
            if entry:
                return str(entry)
        return ""


def _probe_hooks(
    run_dir: Path, manifest_data: dict | None, source_root: Path | None
) -> list[dict]:
    """收集安装期探针该声明的插桩点。

    取并集，口径与 :func:`app.core.ext.enable._hooks_of_item` 一致：

    1. **清单 ``method_hooks``** —— 权威来源，作者声明了哪些方法要插。
       相位**双插**（before + after）：清单里的 phase 常被写成默认值，
       与实际注册不一致（浮游炮清单写 after，源码 before/after 都用）。
       多插一个点只是多派发一次空回调；少插一个点会让
       ``add_method_hook`` 返回 false，而扩展普遍写成
       ``if not add_method_hook(...): return false`` —— 后面的钩子全部丢失。
    2. **载荷源码反查** —— 补上清单没写、但代码里确实注册了的（含用常量
       写脚本路径的写法）。

    **单包边界**：只保留「此刻真能看见的脚本」。指向 ``requires`` 其它扩展
    的钩子在这里必然找不到源码，插了只会让探针误判失败；留给真实启动时的
    合并插桩。找不到脚本的钩子不在这里报错 —— 清单核对
    （``manifest.check_hooks_against_sources``）已经把 target 缺失报成 error 了。
    """
    hooks: dict[tuple[str, str, str], dict] = {}

    def add(script: object, method: object, phase: object) -> None:
        script_text = str(script or "").replace("res://", "")
        method_text = str(method or "")
        phase_text = str(phase or "").strip().lower()
        if not script_text or not method_text:
            return
        if phase_text not in ("before", "after"):
            phase_text = "after"
        hooks.setdefault(
            (script_text, method_text, phase_text),
            {"script": script_text, "method": method_text, "phase": phase_text},
        )

    for h in (manifest_data or {}).get("method_hooks") or []:
        if not isinstance(h, dict):
            continue
        add(h.get("target"), h.get("method"), "before")
        add(h.get("target"), h.get("method"), "after")

    for h in _scan_runtime_hooks(run_dir / "src"):
        add(h.get("script"), h.get("method"), h.get("phase"))

    if source_root is None:
        # 没有反解工程就无从判断「谁看得见」，全带上交给插桩层去跳过的跳过。
        return list(hooks.values())

    payload_root = run_dir / "src" / "payload"
    visible = [
        h
        for h in hooks.values()
        if (source_root / h["script"]).is_file() or (payload_root / h["script"]).is_file()
    ]
    return visible


def _scan_runtime_hooks(src_root: Path) -> list[dict]:
    """扫描扩展载荷里的 ``add_method_hook(...)`` 调用，记录真实相位。

    清单中的 ``method_hooks[].phase`` 常被写成默认值，与实际注册不一致；
    以源码为准才能真正对上插桩点。

    脚本路径既认字符串字面量，也认常量名（``add_method_hook(PLAYER_SCRIPT, ...)``）
    ——见 :func:`app.core.ext.hooks.scan_registrations`。
    """
    files: dict[str, str] = {}
    if not src_root.exists():
        return []
    for path in src_root.rglob("*.gd"):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            files[str(path.relative_to(src_root))] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return [
        {"script": h.script, "method": h.method, "phase": h.phase}
        for h in hooks_mod.scan_tree(files)
    ]


def rescan_runtime_hooks(cfg: dict | None = None) -> list[dict]:
    """重扫**已安装**扩展的 ``runtime_hooks`` 并更新注册表。

    为什么需要这条路径
    ------------------

    ``runtime_hooks`` 是**安装时**扫出来写进注册表的。扫描器升级后
    （比如开始支持常量形式的 ``add_method_hook``），老扩展的注册表里
    仍然是旧的扫描结果——不重装就不会更新。

    而漏掉的钩子有连锁后果：没被扫到 → 没被插桩 → 运行期注册被拒 →
    扩展的 ``install()`` 返回 false → **整个扩展静默失效**
    （实测 ``ai_act.combat_workbench`` 就是这样挂掉的）。

    扩展的源码留在 ``builds/_install_<id>_<line>/src/``（安装时的解包目录），
    所以不用重装包也能重扫。

    返回每条更新的摘要：``[{"id": ..., "line": ..., "before": N, "after": M}]``
    """
    cfg = cfg if cfg is not None else cfg_mod.load()
    svc = ExtensionService(cfg)
    items = svc.installed()
    updates: list[dict] = []
    for item in items:
        ext_id = str(item.get("id") or "")
        if not ext_id:
            continue
        lines = item.get("lines")
        if not isinstance(lines, dict) or not lines:
            continue
        changed = False
        for line_key, line_entry in list(lines.items()):
            if not isinstance(line_entry, dict):
                continue
            src_root = paths.BUILDS / f"_install_{ext_id}_{str(line_key).replace('.', '')}" / "src"
            if not src_root.exists():
                continue
            new_hooks = _scan_runtime_hooks(src_root)
            old_hooks = line_entry.get("runtime_hooks") or []
            if len(new_hooks) == len(old_hooks):
                continue
            line_entry["runtime_hooks"] = new_hooks
            changed = True
            updates.append(
                {
                    "id": ext_id,
                    "line": line_key,
                    "before": len(old_hooks),
                    "after": len(new_hooks),
                }
            )
        if changed:
            # 顶层字段也存着「最近一次安装的那条线」的 runtime_hooks，
            # 得跟 lines 里那份保持一致，否则读顶层的老代码会拿到旧值。
            for line_key, line_entry in lines.items():
                if isinstance(line_entry, dict) and str(item.get("engine_line")) == str(line_key):
                    item["runtime_hooks"] = line_entry.get("runtime_hooks") or []
                    break
    if updates:
        svc._save_registry(items)
    return updates


def service(cfg: dict | None = None) -> ExtensionService:
    return ExtensionService(cfg or cfg_mod.load())

# ---------------------------------------------------------------- 合集包


@dataclass
class BundleItem:
    """合集包里的一个成员。"""

    name: str
    ok: bool
    ext_id: str = ""
    version: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "id": self.ext_id,
            "version": self.version,
            "message": self.message,
        }


@dataclass
class BundleResult:
    """合集包安装结果。"""

    ok: bool
    root: str = ""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    items: list[BundleItem] = field(default_factory=list)
    message: str = ""
    #: 实际安装顺序（按 requires 拓扑排序后）。用于排查「为什么这个包先装了」。
    order: list[str] = field(default_factory=list)
    #: 组合校验 / 预检产生的提示（不算失败，但要让人看见）。
    #: 这个字段以前只在调用点被 `result.warnings.extend(...)` 用到、数据类里
    #: 却没有声明：只要预检剔除了任何一个成员，整批就会因 AttributeError
    #: 直接崩成「安装失败」（2026-11-17 排查导入耗时顺手发现）。
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "root": self.root,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "message": self.message,
            "order": list(self.order),
            "warnings": list(self.warnings),
            "items": [i.as_dict() for i in self.items],
        }


def detect_bundle(package: str | Path) -> list[str]:
    """判断是不是「合集包」：自身没有 extension.json，但内层有多个 .zip。

    返回内层扩展包的名字列表（空列表表示不是合集）。
    真实场景：用户从论坛下到一个 ``mod1028.zip``，里面是 16 个扩展各自一个 zip。
    逐个手动解压再装太蠢，应该一次装完。
    """
    import io
    import zipfile

    pkg = Path(package)
    if not pkg.is_file() or pkg.suffix.lower() != ".zip":
        return []
    try:
        with zipfile.ZipFile(pkg) as outer:
            names = outer.namelist()
            if any(n == "extension.json" or n.endswith("/extension.json") for n in names):
                return []      # 自己就是扩展包
            inner = [n for n in names if n.lower().endswith(".zip")]
            found: list[str] = []
            for name in inner:
                try:
                    with zipfile.ZipFile(io.BytesIO(outer.read(name))) as sub:
                        if any(x == "extension.json" or x.endswith("/extension.json") for x in sub.namelist()):
                            found.append(name)
                except (zipfile.BadZipFile, OSError):
                    continue
            return found
    except (zipfile.BadZipFile, OSError):
        return []


def _bundle_member_meta(outer: "zipfile.ZipFile", name: str) -> tuple[str, list[str]]:
    """读合集内层包的 ``(id, requires)``；读不出来按「无依赖」处理。"""
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(outer.read(name))) as sub:
            candidates = [
                x
                for x in sub.namelist()
                if x == "extension.json" or x.endswith("/extension.json")
            ]
            if not candidates:
                return "", []
            # 取最浅的一份：合集成员偶尔多套一层目录
            data = json.loads(sub.read(min(candidates, key=len)).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, KeyError, ValueError):
        # 只吞「包坏了 / 清单读不了」这类数据问题；编程错误（NameError 等）
        # 必须冒泡，否则依赖会被静默当成空——某个 mod.zip 的 P01 就这么漏过。
        return "", []
    if not isinstance(data, dict):
        return "", []
    ext_id = str(data.get("id") or "")
    reqs: list[str] = []
    for req in data.get("requires") or []:
        rid = str(req.get("id") if isinstance(req, dict) else (req or ""))
        if rid and rid != ext_id and rid not in reqs:
            reqs.append(rid)
    return ext_id, reqs


def order_bundle_members(
    members: list[str], metas: dict[str, tuple[str, list[str]]]
) -> list[str]:
    """按 ``requires`` 排安装顺序：**被依赖的先装**。

    为什么非排不可：安装一个扩展时要算补丁，而补丁的「原文」在目标文件属于
    *另一个扩展* 时，来自 ``packs/<id>/overlay-*.pck``。若 A 改的是 B 的文件、
    A 却先装，B 还没落盘，取原文必然失败——某个 ``mod.zip`` 里
    ``ai_act.dimension_performance``（P01）与 ``rose.dimension_ruins``
    正是这种关系：按目录名字母序打包时 P01 排第 2、次元裂缝排第 18，
    于是 P01 每次都报「补丁目标取不到原文」。

    环、缺失依赖、清单读不出来：一律保持原序，不猜也不丢。
    """
    by_id: dict[str, str] = {}
    for name in members:
        ext_id = metas.get(name, ("", []))[0]
        if ext_id and ext_id not in by_id:
            by_id[ext_id] = name

    placed: set[str] = set()
    ordered: list[str] = []

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if name in placed or name in stack:
            return          # 已排好 / 遇到环：就此打住
        for rid in metas.get(name, ("", []))[1]:
            dep = by_id.get(rid)
            if dep and dep != name:
                visit(dep, stack + (name,))
        if name not in placed:
            placed.add(name)
            ordered.append(name)

    for name in members:
        visit(name, ())
    return ordered


def resolve_source(entry: dict, work_dir: str | Path | None = None) -> Path | None:
    """找回一个已安装扩展的原始包。

    合集安装时成员是从合集包里临时解出来的，临时目录用完就删。所以除了
    ``source_package``，注册表里还记了 ``source_ref``（合集包路径 + 成员名），
    这里据此把成员重新解出来。
    """
    raw = str(entry.get("source_package") or "")
    if raw and Path(raw).is_file():
        return Path(raw)

    ref = entry.get("source_ref")
    if isinstance(ref, dict):
        bundle = str(ref.get("bundle") or "")
        member = str(ref.get("member") or "")
        if bundle and member and Path(bundle).is_file():
            import io
            import zipfile

            try:
                with zipfile.ZipFile(bundle) as outer:
                    data = outer.read(member)
            except (OSError, KeyError, zipfile.BadZipFile):
                return None
            target = Path(work_dir) / f"{entry.get('id', 'ext')}.zip" if work_dir else None
            if target is None:
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return target
    return None


def _partial_can_install(exc: Exception, line: str, allow_partial: bool) -> bool:
    """判断「生成覆盖层失败」是否属于「部分可用」，可以降级安装。

    **保守原则：判不出来就返回 False（维持拒装）。**
    宁可少装一个，也不要把一个注定空壳的包装上去 —— 后者玩家更难查。

    可以降级的三种情况：

    1. 异常消息里明确是「锚点/上下文不匹配」这类**局部**失配
       （``补丁上下文不匹配`` / ``载荷缺文件`` / ``命中 0``）；
    2. 调用方已经开了 ``allow_partial``（广谱兼容跨版本安装）；
    3. 该线是 4.7 —— 剧情版会随版本更新，旧 mod 必然失配，
       一律拒装等于让 4.7 生态无法存活。

    **不可降级的**：清单读不出来、覆盖层写盘失败、pck 结构损坏 ——
    这些装上去就是个空壳或坏包。
    """
    text = f"{type(exc).__name__}: {exc}".lower()

    # 结构性失败：无论如何都不装
    fatal_markers = (
        "manifest", "清单", "unsupported", "不支持",
        "permission denied", "拒绝访问", "no space", "磁盘",
        "pck", "write", "写入失败",
    )
    if any(m in text for m in fatal_markers):
        return False

    if allow_partial:
        return True

    # 局部失配的典型措辞（见 ext/apply.py 的 raise 语句）
    partial_markers = (
        "上下文不匹配", "锚点", "anchor",
        "载荷缺文件", "命中 0", "没命中",
        "patch", "补丁",
    )
    if any(m in text for m in partial_markers):
        return True

    # 4.7 兜底：剧情更新导致的失配形式难以穷举（可能来自
    # compose / apply / patcher 任意一层），这条线一律降级安装。
    # 装上后 effect-report.json 会记下哪几处没生效，玩家能查到。
    if str(line or "") == "4.7":
        return True

    return False


def _member_requires(ext_id: str) -> set[str]:
    """查某个已装扩展的 ``requires`` 依赖集合。

    用于合集「部分成功」时的依赖闭包判定：B 依赖 A，A 装失败，
    则 B 也算失败 —— 否则 B 装上也是废的（它的补丁依赖 A 的改动）。

    查不到（包里没有记录）时返回空集：**不能因为查不到就假定它有依赖**，
    那会把无关的包一起回滚，正是我们要避免的。
    """
    try:
        from .. import paths

        reg_dir = getattr(paths, "CONFIG_DIR", None)
        if reg_dir is None:
            return set()
        reg_path = Path(reg_dir) / "extensions.json"
        if not reg_path.is_file():
            return set()
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 读不到就不做闭包，不影响主流程
        return set()
    for item in data.get("items") or []:
        if not isinstance(item, dict) or str(item.get("id") or "") != ext_id:
            continue
        raw = item.get("requires")
        if isinstance(raw, dict):
            return {str(k) for k in raw}
        if isinstance(raw, list):
            return {str(x) for x in raw}
        return set()
    return set()


def _bundle_preflight(
    svc: "ExtensionService",
    outer,
    members: list[str],
    temp_root: Path,
    target_line: str | None,
    say,
) -> dict | None:
    """开发者校验下的合集包**成员级**预检。

    先把所有成员解出来、读清单、和「当前已启用 + 本批成员」做一次完整
    组合解析。返回 ``None`` 表示全部通过。

    **2026-11-08 改：从「整包预检」变成「成员级预检」。**

    原来的行为是一刀切：任何一个成员有静态错误或组合冲突，返回一条
    「整包不安装」，25 个包全废。

    实测后果：25 个包里 5 个因为隔离规则误判，剩下 20 个**一个都没装成**。
    玩家明明有 20 个能用的包，什么都得不到。

    现在的行为：返回 ``{"exclude": {成员名: 原因}, "warn": [...]}``，
    调用方把 ``exclude`` 里的成员跳过，其余照装。

    保留的硬约束：
      - 同名包解出失败 / 清单解析失败 → 只排除它自己；
      - 组合解析里的 error 级问题 → 只排除**问题里点名的包**；
      - 未被点名的成员一律放行（不是「没问题」，而是「不该替它背锅」）。
    """
    from . import enable as enable_mod
    from . import resolver as resolver_mod

    probes: list[tuple[str, Path, object]] = []
    exclude: dict[str, str] = {}
    warns: list[str] = []
    for name in members:
        member_path = temp_root / Path(name).name
        try:
            member_path.write_bytes(outer.read(name))
        except (OSError, KeyError) as exc:
            exclude[name] = f"解出失败：{exc}"
            continue
        try:
            probe = ext_manifest.load(member_path)
        except Exception as exc:  # noqa: BLE001
            exclude[name] = f"清单解析失败：{exc}"
            continue
        hard = [i for i in probe.issues if i.level == "error"]
        if hard:
            exclude[name] = "；".join(i.message for i in hard[:3])
            continue
        probes.append((name, member_path, probe))

    if not probes:
        # 一个都没解出来：退化成一个成员都没有，交给调用方报「全部失败」
        return {"exclude": exclude, "warn": warns}

    line = target_line or "4.6"
    ids = {str(probe.id) for _, _, probe in probes}
    pairs = [
        (pid, data)
        for pid, data in enable_mod.enabled_manifests(svc.cfg, line)
        if pid not in ids
    ]
    pairs += [(str(probe.id), probe.data) for _, _, probe in probes]
    resolution = resolver_mod.resolve(
        pairs,
        compose=cfg_mod.extension_compose(svc.cfg),
        mode="strict",
        line=line,
        store=decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json"),
    )
    hard = [
        p
        for p in resolution.problems
        if p.level == "error" and any(str(x) in ids for x in (p.ids or []))
    ]
    if not hard:
        return {"exclude": exclude, "warn": warns}

    # 只排除「问题里点名的」成员，其余照装。
    #
    # 为什么按 id 排除而不是「有 error 就全排除」：一条冲突至少涉及两个
    # 包，但可能其中一个是玩家早就装好的老包 —— 那个不该被本批连累。
    # 只有当出问题的 id 确实在本批里，才把它从本批剔除。
    by_id = {str(probe.id): name for name, _, probe in probes}
    for problem in hard:
        blame = {str(x) for x in (problem.ids or []) if str(x) in ids}
        if not blame:
            continue
        for pid in blame:
            name = by_id.get(pid)
            if not name or name in exclude:
                continue
            exclude[name] = str(problem.message)
    if exclude:
        warns.append(
            f"组合检查：{len(exclude)} 个成员被排除，其余 {len(probes) - len(exclude)} 个照装"
        )
    return {"exclude": exclude, "warn": warns}


def _verify_bundle_smoke(cfg: dict, line: str, timeout: float, say) -> dict:
    """合集装完后的**组合运行校验**：整线一次真引擎（替代逐包冒烟）。

    为什么要单独包一层：``verify_line`` 会组装整条线的启动包（含插桩层），
    任何一环炸掉都不该把「已经装好的扩展」判成安装失败。这里把异常收敛成
    ``ok=False`` + 可读原因，让调用方按「点不到具体包就不回滚」处理。

    返回 :func:`app.core.ext.verify.verify_line` 的结果字典。
    """
    from . import verify as verify_mod

    try:
        out = verify_mod.verify_line(cfg, line, timeout)
    except Exception as exc:  # noqa: BLE001 - 诊断失败不能反噬安装结果
        return {
            "ok": False,
            "loaded": [],
            "failed": [],
            "missing": [],
            "problems": [f"组合校验未能执行：{exc}"],
            "reason": f"组合校验未能执行：{exc}",
            "seconds": 0.0,
        }
    say(
        96,
        f"组合校验完成：装载 {len(out.get('loaded') or [])} 个"
        f"（{out.get('seconds', 0)} 秒）",
    )
    return out


def install_bundle(
    cfg: dict | None,
    package: str | Path,
    progress: Progress | None = None,
    *,
    verify_smoke: bool = False,
    timeout: float = 240.0,
    on_item=None,
    target_line: str | None = None,
    allow_partial: bool = False,
) -> BundleResult:
    """把一个合集包里的所有扩展逐个装上。

    ``verify_smoke`` 默认关闭：合集里常有十几个包，逐个跑冒烟要几分钟。
    装完可以单独对关心的包再验。``on_item(BundleItem)`` 每装完一个回调一次。

    ``target_line`` 强制目标引擎线（默认按各包自己的 baseline 推断）。
    ``allow_partial`` 允许补丁只命中一部分也算装上。
    """
    import io
    import tempfile
    import zipfile

    pkg = Path(package)
    result = BundleResult(ok=False, root=str(pkg))
    say = progress or (lambda _p, _l: None)

    members = detect_bundle(pkg)
    if not members:
        result.message = "不是合集包（自身没有 extension.json，内层也没有多个扩展 zip）"
        return result

    result.total = len(members)
    svc = ExtensionService(cfg or cfg_mod.load())
    mode = cfg_mod.extension_mode(svc.cfg)
    installed_here: list[str] = []
    temp_root = paths.BUILDS / f"_bundle_{pkg.stem}"
    if temp_root.exists():
        shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(pkg) as outer:
        # 先按 requires 排安装顺序：被依赖的要先落盘，否则后面那个包
        # 在算补丁时读不到「另一个扩展的文件」（P01 与次元裂缝就是这样）。
        metas = {name: _bundle_member_meta(outer, name) for name in members}
        ordered = order_bundle_members(members, metas)
        if ordered != members:
            say(0, "按依赖调整安装顺序：" + " → ".join(Path(n).name for n in ordered))
        members = ordered
        result.order = [Path(n).name for n in members]

        # 完全校验：先做**成员级**预检，只排除有问题的成员，其余照装。
        # （原来是一刀切整包不装 —— 25 个包里 5 个有问题就全废，
        #   玩家明明有 20 个能用的包却什么都得不到。2026-11-08 改）
        if cfg_mod.is_strict(mode):
            preflight = _bundle_preflight(
                svc, outer, members, temp_root, target_line, say
            )
            if preflight:
                excluded = preflight.get("exclude") or {}
                for name in members:
                    reason = excluded.get(name)
                    if reason is None:
                        continue
                    item = BundleItem(
                        name=Path(name).name, ok=False, message=reason
                    )
                    result.items.append(item)
                    result.failed += 1
                    if on_item:
                        try:
                            on_item(item)
                        except Exception:  # noqa: BLE001
                            pass
                if excluded:
                    # 从待装列表里去掉，剩下的照常走安装流程
                    members = [n for n in members if n not in excluded]
                    result.warnings.extend(preflight.get("warn") or [])
                    say(
                        10,
                        f"预检排除 {len(excluded)} 个，其余 {len(members)} 个继续安装",
                    )
                if not members:
                    result.ok = False
                    result.message = (
                        f"合集共 {result.total} 个：预检全部未通过，没有可安装的成员"
                    )
                    shutil.rmtree(temp_root, ignore_errors=True)
                    say(100, result.message)
                    return result

        # 完全校验 + 多个成员：**不逐包冒烟**，装完统一跑一次组合冒烟。
        #
        # 为什么改（2026-11-17 实测）：逐包冒烟的代价是「每包一次真引擎启动」，
        # 4.7 的 21 个成员实测 21 × 21.5 秒 ≈ 7.5 分钟，而这 21 次启动里有
        # 20 次验证的都是玩家根本不会跑的配置（只挂一个包）。
        # 换成一次「全部已启用扩展一起挂」，走的是启动时同一套组装路径
        # （enable.build_for_launch + 插桩层），既快又更贴近真实启动。
        # 单包导入仍然保留逐包冒烟：那条路径只有一次，必须自证。
        defer_smoke = cfg_mod.is_strict(mode) and len(members) > 1
        smoke_line = target_line or ""
        # 冒烟延后时给组合校验留出最后 10%：否则成员装完进度先到 100%，
        # 再回落到「组合校验」的 96%，看起来像退回去了。
        span_total = 90.0 if defer_smoke else 100.0

        for index, name in enumerate(members, start=1):
            base = span_total * (index - 1) / len(members)
            span = span_total / len(members)
            say(int(base), f"[{index}/{len(members)}] {Path(name).name}")

            member_path = temp_root / Path(name).name
            try:
                member_path.write_bytes(outer.read(name))
            except (OSError, KeyError) as exc:
                item = BundleItem(name=Path(name).name, ok=False, message=f"解出失败：{exc}")
                result.items.append(item)
                result.failed += 1
                if on_item:
                    on_item(item)
                continue

            def member_progress(percent: int, label: str, _base=base, _span=span, _name=name) -> None:
                say(int(_base + _span * percent / 100), f"{Path(_name).name} · {label}")

            try:
                one = svc.install(
                    member_path,
                    member_progress,
                    # 合集里多成员时冒烟延后到整批装完统一跑一次；
                    # 宽松档沿用调用方设置（本来就不跑冒烟）。
                    verify_smoke=(
                        False
                        if defer_smoke
                        else (True if cfg_mod.is_strict(mode) else verify_smoke)
                    ),
                    timeout=timeout,
                    target_line=target_line,
                    allow_partial=allow_partial,
                    source_ref={
                        "package": str(pkg),
                        "member": name,
                        "bundle": str(pkg),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 单个包失败不能中断整批
                one = InstallResult(ok=False, message=f"{type(exc).__name__}: {exc}")

            item = BundleItem(
                name=Path(name).name,
                ok=one.ok,
                ext_id=one.ext_id,
                version=one.version,
                message=one.message,
            )
            result.items.append(item)
            if one.ok:
                result.succeeded += 1
                if one.ext_id and one.ext_id not in installed_here:
                    installed_here.append(one.ext_id)
                if not smoke_line:
                    smoke_line = str(getattr(one, "engine_line", "") or "")
            else:
                result.failed += 1
            if on_item:
                try:
                    on_item(item)
                except Exception:  # noqa: BLE001
                    pass

    shutil.rmtree(temp_root, ignore_errors=True)

    # ---------------------------------------------------------------
    # 组合运行校验（替代逐包冒烟）：一次引擎，点名到包
    #
    # 判据不是「全部通过」，而是「本批新装的这些能不能一起跑」：
    #   1. 通过 → 正常收尾；
    #   2. 有本批成员在 mod_report 里被点名（failed / missing）→ 只回滚它们，
    #      再验一次确认剩下的这套是干净的；
    #   3. 失败但点不到本批成员（老包坏了 / 启动期脚本错误）→ 不回滚，
    #      把结论写进 warnings，交给「整体校验」深挖。
    # 第 2 步的回滚是**单包**的，不牵连别人 —— 与「允许部分成功」的既定
    # 决策一致（2026-11-08：20 个能用就不该因为 1 个坏了全废）。
    # ---------------------------------------------------------------
    if defer_smoke and installed_here:
        verify_line_name = smoke_line or "4.6"
        # 这一步要真启动一次引擎（3 GB 本体，约 30 秒），必须先说明在做什么，
        # 否则进度条会长时间停在 90% 不动。
        say(91, f"组合运行校验：{len(installed_here)} 个扩展一起挂载（约 30 秒）")
        verdict = _verify_bundle_smoke(svc.cfg, verify_line_name, timeout, say)
        bad: set[str] = set()
        for rec in verdict.get("failed") or []:
            bad.add(str((rec or {}).get("id") or ""))
        bad.update(str(x) for x in (verdict.get("missing") or []))
        bad.discard("")
        rolled = [ext_id for ext_id in installed_here if ext_id in bad]
        final_ok = bool(verdict.get("ok"))
        if rolled:
            for ext_id in rolled:
                try:
                    svc.uninstall(ext_id)
                except Exception:  # noqa: BLE001 - 回滚失败不影响结论
                    pass
                installed_here.remove(ext_id)
                for it in result.items:
                    if str(it.ext_id or "") == ext_id and it.ok:
                        it.ok = False
                        it.message = "组合运行校验未通过，已回滚"
                        result.succeeded -= 1
                        result.failed += 1
            result.warnings.append(
                f"组合运行校验：{len(rolled)} 个扩展装完起不来，已回滚"
                f"（{verdict.get('reason') or '未通过'}）"
            )
            say(
                97,
                f"组合校验回滚 {len(rolled)} 个，复验其余 {len(installed_here)} 个",
            )
            again = _verify_bundle_smoke(svc.cfg, verify_line_name, timeout, say)
            final_ok = bool(again.get("ok"))
            if not final_ok:
                result.warnings.append(
                    "复验仍未通过：" + (again.get("reason") or "未知原因")
                )
        elif not final_ok:
            # 不是本批的问题：不清空玩家已经装好的扩展，只如实报出结论。
            reason = str(verdict.get("reason") or "未知原因")
            if "未能执行" in reason:
                # 连引擎都没起来（基线没导入 / 组装失败）：别说成「跑不过」
                result.warnings.append(
                    f"组合运行校验未能执行：{reason}（本次只做了文件层校验）"
                )
            else:
                result.warnings.append(
                    f"组合运行校验未通过（问题不在本批新装的扩展上）：{reason}"
                )
        if final_ok and installed_here:
            # 注册表里的 verified 跟着结论走：这些包的「能跑」是整批一起
            # 真跑出来的，不是「文件层读回来一致」。
            svc.mark_verified(installed_here, verify_line_name)

    # ---------------------------------------------------------------
    # 合集结果：**允许部分成功**（2026-11-08 用户决策）
    #
    # 原来是一刀切整包事务：只要有一个成员失败，就把本次装好的全部卸载，
    # 报告「整包未通过，已全部回滚」。
    #
    # 后果实测：25 个包里有 5 个被误判，剩下 20 个**一个都没装成**。
    # 对玩家来说是灾难 —— 他明明有 20 个能用的包，却什么都得不到。
    #
    # 现在的规则：
    #   1. 装成的留着，失败的单列出来，逐条可查；
    #   2. **但依赖闭包要连带**：B requires A，A 失败则 B 也标失败 ——
    #      否则 B 装上也是废的（它的补丁依赖 A 的改动）。
    #   3. 回滚只针对「装到一半出错」的单个包，不牵连别人。
    # ---------------------------------------------------------------
    if result.failed:
        # 依赖闭包：把「依赖了失败包」的成员也标失败。
        # 用 installed_here 里真正成功的做基底，反查 failed 里的 id。
        failed_ids = {
            str(it.ext_id or "") for it in result.items if not it.ok and it.ext_id
        }
        for it in result.items:
            if not it.ok or not it.ext_id or it.ext_id in failed_ids:
                continue
            # 这个成员成功了，但它依赖的包失败了 → 它也不该留着
            deps = _member_requires(it.ext_id)
            broken = deps & failed_ids
            if not broken:
                continue
            try:
                svc.uninstall(it.ext_id)
            except Exception:  # noqa: BLE001 - 回滚失败不影响结论
                pass
            it.ok = False
            it.message = (
                f"已回滚：依赖的 {'、'.join(sorted(broken))} 未装上"
            )
            result.succeeded -= 1
            result.failed += 1
            if it.ext_id in installed_here:
                installed_here.remove(it.ext_id)

    result.ok = result.succeeded > 0
    if result.failed:
        ok_names = [str(it.name) for it in result.items if it.ok]
        bad_names = [str(it.name) for it in result.items if not it.ok]
        result.message = (
            f"合集共 {result.total} 个：成功 {result.succeeded}，失败 {result.failed}"
            + (f"。未装上：{'、'.join(bad_names[:6])}" if bad_names else "")
            + ("…" if len(bad_names) > 6 else "")
            + (f"；已装上 {len(ok_names)} 个，可正常使用" if ok_names else "")
        )
    else:
        result.message = f"合集共 {result.total} 个：全部成功"
    say(100, result.message)
    return result


# ---------------------------------------------------------------- 批量安装


@dataclass
class BatchItem:
    """批量安装里的一个包（可能是单个扩展，也可能是合集包）。"""

    package: str
    ok: bool
    kind: str = "single"          # single / bundle
    ext_id: str = ""
    name: str = ""
    version: str = ""
    message: str = ""
    conflicts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "package": self.package,
            "ok": self.ok,
            "kind": self.kind,
            "id": self.ext_id,
            "name": self.name,
            "version": self.version,
            "message": self.message,
            "conflicts": self.conflicts,
        }


@dataclass
class BatchResult:
    """一次批量安装的汇总。"""

    ok: bool = False
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    items: list[BatchItem] = field(default_factory=list)
    #: 整批装完后对「新装的这些 + 已启用」做的组合检查结论
    conflicts: list[dict] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "message": self.message,
            "items": [i.as_dict() for i in self.items],
            "conflicts": self.conflicts,
        }

    def failures(self) -> list[BatchItem]:
        return [i for i in self.items if not i.ok]


@dataclass
class UninstallReport:
    """批量卸载结果：删掉了谁、谁本来就不在、哪些目录没删干净。"""

    ok: bool
    removed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "removed": self.removed,
            "missing": self.missing,
            "warnings": self.warnings,
            "message": self.message,
        }


@dataclass
class PreviewResult:
    """导入前预览：不写盘的静态校验 + 组合比对结论。"""

    ok: bool
    ext_id: str = ""
    name: str = ""
    version: str = ""
    engine_line: str = ""
    issues: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    message: str = ""
    #: 开发者校验专有：**实际合成一遍**发现的锚点失配。
    #:
    #: 位置级处置（``decisions.json``）能让重叠在规则上「合法」，但规则合法
    #: 不等于补丁真能落地——replace 钩子把函数体搬走、别人拿函数体开头当锚点，
    #: 这条在规则层看不出来，只有真跑一遍合成才知道命中 0 次。
    #: 2026-11-05 主城灰屏就是这么漏过去的：12 处重叠全被处置接住，装包显示
    #: 成功，进游戏主城一片灰。这里补上「真合成」这一道。
    anchor_failures: list[dict] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """是否应拒绝导入：硬冲突或（严格档下）锚点会失配。"""
        return not self.ok

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "id": self.ext_id,
            "name": self.name,
            "version": self.version,
            "engine_line": self.engine_line,
            "issues": self.issues,
            "conflicts": self.conflicts,
            "anchor_failures": self.anchor_failures,
            "message": self.message,
        }


def _conflict_summary(conflicts: list[dict]) -> str:
    """把组合检查结论压成一句，拼在安装成功的消息后面。

    必须**总是**给一句，哪怕是「未发现冲突」——否则用户无法区分
    「检查过没问题」和「根本没检查」，这正是过去最容易误判的地方。
    """
    if not conflicts:
        return "；与已装扩展没有冲突"
    errors = [c for c in conflicts if c.get("level") == "error"]
    warns = [c for c in conflicts if c.get("level") == "warn"]
    if errors:
        return f"；与已装扩展有 {len(errors)} 处无法共存，启动时会跳过其中一个"
    return f"；与已装扩展有 {len(warns)} 处重叠，已自动处理"


def _post_install_conflicts(cfg: dict, ext_ids: set[str]) -> list[dict]:
    """装完之后再解析一次组合，只保留「新装的包参与」的问题。

    为什么不复用 ``install`` 里的单包冒烟：冒烟只回答「这一个包能不能
    装起来」，看不到组合。这里做的是静态解析，毫秒级、无副作用，
    可以在每次安装结束时都跑。
    """
    from . import enable as enable_mod
    from . import resolver as resolver_mod

    wanted = {str(i) for i in ext_ids if i}
    if not wanted:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for line in cfg_mod.known_lines(cfg):
        pairs = enable_mod.enabled_manifests(cfg, line)
        if not any(pid in wanted for pid, _data in pairs):
            continue
        resolution = resolver_mod.resolve(
            pairs,
            compose=cfg_mod.extension_compose(cfg),
            mode=cfg_mod.extension_mode(cfg),
            line=line,
            store=decisions_mod.Store.load(paths.CONFIG_DIR / "decisions.json"),
        )
        for problem in resolution.problems:
            if problem.level not in ("error", "warn"):
                continue
            if not (wanted & set(problem.ids)):
                continue
            key = (line, problem.code, tuple(sorted(problem.ids)))
            if key in seen:
                continue
            seen.add(key)
            entry = problem.as_dict()
            entry["line"] = line
            out.append(entry)
    return out


def install_any(
    cfg: dict,
    package: str | Path,
    progress: Progress | None = None,
    *,
    verify_smoke: bool = True,
    timeout: float = 240.0,
    target_line: str | None = None,
    allow_partial: bool = False,
) -> "InstallResult | BundleResult | ImportResult":
    """装一个「包」——自动分辨是剧情包、单个扩展还是合集。

    把这段分流逻辑从 ``InstallWorker.run`` 提到 core，是为了让批量安装
    在后台线程之外也能复用同一套判断（CLI、测试、未来的脚本）。UI 的
    ``InstallWorker`` 保持原样，跨线安装与批量升级仍走它。

    1.4 起剧情功能已整体移除，所以这里不再有「剧情包」这一支：
    含 ``story.pck`` 的包会走到 ``ext_manifest.load`` 并报「找不到
    extension.json」，这是预期行为——玩家不该再拿到剧情包。
    """
    pkg = Path(package)
    if detect_bundle(pkg):
        return install_bundle(
            cfg,
            pkg,
            progress,
            verify_smoke=verify_smoke,
            timeout=timeout,
            target_line=target_line,
            allow_partial=allow_partial,
        )
    return ExtensionService(cfg).install(
        pkg,
        progress,
        verify_smoke=verify_smoke,
        timeout=timeout,
        target_line=target_line,
        allow_partial=allow_partial,
    )


def install_many(
    cfg: dict,
    packages,
    progress: Progress | None = None,
    *,
    verify_smoke: bool = False,
    timeout: float = 240.0,
    target_line: str | None = None,
    allow_partial: bool = False,
    on_item=None,
) -> BatchResult:
    """一次装多个包：串行、互不牵连、最后给汇总。

    三个刻意的取舍：

    1. **串行**。安装中间目录 ``builds/_install_<id>_<line>`` 与
       ``packs/<id>`` 都按扩展 id 命名，并发会互相覆盖。
    2. **单个失败不中断**。批量里最怕「第 3 个坏了，后面 7 个都没装」——
       用户还得重新挑一遍。
    3. **N==1 时仍然跑冒烟**。单个导入是「装完就要玩」的主路径，
       跳过冒烟会让「装了却用不了」重新变成常态；只有真的批量（N≥2）
       才为了速度让步，装完再用「整体校验」兜底。
    """
    pkg_list = [Path(p) for p in (packages or [])]
    result = BatchResult(total=len(pkg_list))
    say = progress or (lambda _p, _l: None)
    if not pkg_list:
        result.message = "没有选择任何扩展包"
        return result

    total = len(pkg_list)
    single_package = total == 1
    effective_smoke = True if (single_package and not verify_smoke) else verify_smoke

    for index, pkg in enumerate(pkg_list, start=1):
        base = 100 * (index - 1) / total
        span = 100 / total
        say(int(base), f"[{index}/{total}] {pkg.name}")

        def inner(percent: int, label: str, _b=base, _s=span, _idx=index, _name=pkg.name) -> None:
            say(int(_b + _s * int(percent) / 100), f"[{_idx}/{total}] {_name} · {label}")

        try:
            one = install_any(
                cfg,
                pkg,
                inner,
                verify_smoke=effective_smoke,
                timeout=timeout,
                target_line=target_line,
                allow_partial=allow_partial,
            )
        except Exception as exc:  # noqa: BLE001 - 单个包异常不能中断整批
            one = InstallResult(ok=False, message=f"{type(exc).__name__}: {exc}")

        item = BatchItem(
            package=str(pkg),
            ok=bool(getattr(one, "ok", False)),
            kind="bundle" if isinstance(one, BundleResult) else "single",
            ext_id=str(getattr(one, "ext_id", "") or ""),
            name=str(getattr(one, "name", "") or pkg.name),
            version=str(getattr(one, "version", "") or ""),
            message=str(getattr(one, "message", "") or ""),
            conflicts=list(getattr(one, "conflicts", []) or []),
        )
        result.items.append(item)
        if item.ok:
            result.succeeded += 1
        else:
            result.failed += 1
        if on_item:
            try:
                on_item(item)
            except Exception:  # noqa: BLE001 - 回调出错不影响安装
                pass

    # 整批装完再做一次组合检查：包与包之间可能互相冲突，单个安装时
    # 各自都看不到对方（第 1 个装的时候第 2 个还不存在）。
    ext_ids = {i.ext_id for i in result.items if i.ok and i.ext_id}
    if ext_ids:
        try:
            result.conflicts = _post_install_conflicts(cfg, ext_ids)
        except Exception:  # noqa: BLE001
            result.conflicts = []

    result.ok = result.failed == 0 and result.succeeded == total
    head = f"共 {total} 个：成功 {result.succeeded}，失败 {result.failed}"
    if result.conflicts:
        head += _conflict_summary(result.conflicts)
    elif ext_ids:
        head += "；组合检查未发现冲突"
    result.message = head
    say(100, head)
    return result
