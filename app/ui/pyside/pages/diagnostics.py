"""诊断页：环境体检 + 基线校验 + 底座重建 + 冒烟测试。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....core import config as cfg_mod
from ....core import paths, runner
from ....core.hashing import sha256_file
from .. import colors
from .. import features
from ..widgets import Card, PageHeader, Pill


class DiagnosticsPage(QWidget):
    def __init__(self, cfg: dict, log, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._log = log
        #: 是否展开明细。初值取设置里的「展示模式」（``ui.show_detail``）。
        #:
        #: 默认简洁：玩家只想知道「这两个版本能不能玩」，
        #: 正常时铺 11 行技术细节只会让这个页面显得复杂。
        #: 开发者/排查问题的人去「设置 → 展示模式」打开详细展示。
        self.show_details = features.show_detail(cfg)
        #: 上一次从设置里读到的值。用来识别「设置被改过」，
        #: 改了就以设置为准刷新显示（见 ``refresh`` 开头）。
        self._last_configured_detail = self.show_details

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        header = PageHeader("诊断", "先看体检表；异常项再去跑校验或冒烟定位。")
        self.state_pill = Pill("未检查", "muted")
        header.add_action(self.state_pill)
        # 折叠 / 展开明细。默认折叠，出问题的线会自动展开，
        # 这个按钮是给「想主动看全部技术细节」的人用的。
        self.detail_btn = QPushButton("收起明细" if self.show_details else "展开明细")
        self.detail_btn.setProperty("variant", "ghost")
        self.detail_btn.setToolTip(
            "默认按设置里的「展示模式」决定（设置 → 展示模式）。\n"
            "简洁模式：每个游戏版本一行，出问题的那行会自动展开。\n"
            "详细模式：始终显示全部技术细节。\n\n"
            "这个按钮只是临时切换当前页面的显示，不改设置。"
        )
        self.detail_btn.clicked.connect(self._toggle_details)
        header.add_action(self.detail_btn)
        refresh = QPushButton("重新体检")
        refresh.setProperty("variant", "ghost")
        refresh.clicked.connect(self.refresh)
        header.add_action(refresh)
        root.addWidget(header)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["检查项", "状态", "详情"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)

        actions_card = Card()
        row = QHBoxLayout(actions_card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(8)
        self.verify_btn = QPushButton("校验基线指纹")
        self.verify_btn.clicked.connect(self._verify)
        self.base_btn = QPushButton("重建兼容底座")
        self.base_btn.clicked.connect(self._rebuild_base)
        self.drift_btn = QPushButton("源码漂移检测")
        self.drift_btn.clicked.connect(self._drift)

        # 冒烟要能选线：**1.7 起有两条引擎线，只对 4.6 做冒烟等于 4.7 从来没验过**。
        # 原实现是 `lambda: self._smoke(cfg_mod.BUILTIN_LINE)`，4.7 玩家
        # 在这个页面上找不到任何能对 4.7 执行的动作。
        self.smoke_line = QComboBox()
        for line in self._lines():
            self.smoke_line.addItem(line, line)
        self.smoke_line.setFixedWidth(72)
        self.smoke_btn = QPushButton("冒烟")
        self.smoke_btn.clicked.connect(
            lambda: self._smoke(str(self.smoke_line.currentData() or cfg_mod.BUILTIN_LINE))
        )
        for btn in (self.verify_btn, self.base_btn, self.drift_btn, self.smoke_line, self.smoke_btn):
            row.addWidget(btn)
        row.addStretch(1)
        root.addWidget(actions_card)

    # ------------------------------------------------------------ 体检

    def _identity_row(self, line: str, bid: str) -> list[tuple[str, str, str, str]]:
        """分层指纹的一行摘要。

        只对**有 identity 且含 code_layer** 的基线出这一行 —— 4.6 的
        identity 没有代码层指纹（那是 1.7 新加的），硬列一行「未知」
        只会让人以为坏了。
        """
        from ....core import baseline_identity as bi

        try:
            ident = bi.load_identity(self.cfg, line)
        except Exception:  # noqa: BLE001
            return []
        if not ident:
            return []
        code = ident.get("code_layer") or {}
        if not code.get("digest"):
            return []
        layer_names = "、".join((ident.get("layers") or {}).keys()) or "body"
        return [
            (
                f"指纹 {bid}",
                "ok",
                f"代码层 {len(code.get('files') or {})} 个文件，digest {str(code['digest'])[:12]}…；"
                f"整包层：{layer_names}",
            )
        ]

    def _lines(self) -> tuple[str, ...]:
        """这台机器上真实登记的引擎线（按配置，不写死）。

        原先全页有 6 处写死 ``BUILTIN_LINE`` / ``("1.45",)`` / ``("46",)``，
        双线之后 4.7 在诊断页完全不可见 —— 引擎、基线、索引、运行时、
        漂移、冒烟，一条都没有。玩家切到 4.7 想看「到底缺什么」时，
        这个页面反而什么也不说。

        改成读 ``known_lines``：配置里登记了几条线就显示几条。
        """
        lines = [str(x) for x in cfg_mod.known_lines(self.cfg) if str(x)]
        return tuple(lines) or (cfg_mod.BUILTIN_LINE,)

    def _baselines(self) -> list[tuple[str, str]]:
        """``[(engine_line, baseline_id), ...]``，按配置动态取。"""
        out: list[tuple[str, str]] = []
        baselines = self.cfg.get("baselines") or {}
        for line in self._lines():
            for bid, meta in baselines.items():
                if str((meta or {}).get("engine") or "") == line:
                    out.append((line, str(bid)))
        return out

    def _last_effect_report(self, line: str) -> dict | None:
        """最近一次启动时，这条线的「补丁生效报告」。

        由 ``enable.build_for_launch`` 写到 ``builds/<run>/effect-report.json``。
        取最新的一份（按 mtime），因为玩家关心的是「上次启动到底生效了没有」，
        不是历史。

        读不到就返回 ``None``（没启动过 / 老版本没写这个文件），
        此时不显示这一项 —— 没有信息好过给一条「未知」。
        """
        import json

        builds = getattr(paths, "BUILDS", None)
        if builds is None or not Path(builds).is_dir():
            return None
        try:
            candidates = sorted(
                (p for p in Path(builds).rglob("effect-report.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for path in candidates[:8]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(data.get("line") or "") == line:
                return data
        return None

    def _group_by_line(
        self, rows: list[tuple[str, str, str, str]]
    ) -> list[tuple[tuple[str, str, str], list[tuple[str, str, str, str]]]]:
        """把明细行按引擎线归组，每组生成一行汇总。

        返回 ``[(汇总行, 该线的明细行列表), ...]``。

        汇总行用的是玩家认得的名字（「机甲旋风 4.7 · 剧情版」），
        不是内部线号 —— 玩家记不住「4.7 线」对应哪个版本，但认得标题。

        归组依据是每行名字的**后缀**：明细行的名字都以线号结尾
        （``4.7 引擎`` / ``运行时 4.7`` / ``源码漂移 4.7``），
        基线与索引行则按「这条线登记了哪些基线」反查。
        """
        from .library import VERSIONS

        titles = {v["key"]: v["title"] for v in VERSIONS}
        lines = list(self._lines())
        baseline_of = {line: [] for line in lines}
        for line, bid in self._baselines():
            baseline_of.setdefault(line, []).append(bid)
        suffixes = {bid.split(".v")[-1] for _line, bid in self._baselines()}

        buckets: dict[str, list[tuple[str, str, str, str]]] = {line: [] for line in lines}
        other: list[tuple[str, str, str, str]] = []

        for row in rows:
            name = row[0]
            hit = None
            # ① 名字里带线号（"4.7 引擎" / "运行时 4.7" / "源码漂移 4.7"）
            for line in lines:
                if name.endswith(line) or name.startswith(f"{line} "):
                    hit = line
                    break
            # ② 基线 / 指纹 / 索引行按基线 id 反查
            if hit is None:
                for line, bids in baseline_of.items():
                    for bid in bids:
                        suffix = bid.split(".v")[-1]
                        if bid in name or (f" {suffix}" in name):
                            hit = line
                            break
                    if hit:
                        break
            if hit is None:
                other.append(row)
            else:
                buckets.setdefault(hit, []).append(row)

        out: list[tuple[tuple[str, str, str], list[tuple[str, str, str, str]]]] = []
        for line in lines:
            details = buckets.get(line) or []
            if not details:
                continue
            tones = [r[1] for r in details]
            if "err" in tones:
                tone = "err"
            elif "warn" in tones:
                tone = "warn"
            else:
                tone = "ok"

            bad_names = [r[0] for r in details if r[1] != "ok"]
            if tone == "ok":
                summary = f"{len(details)} 项检查全部正常"
            else:
                summary = f"{len(bad_names)} 项需注意：{'、'.join(bad_names[:4])}"
                if len(bad_names) > 4:
                    summary += f" 等 {len(bad_names)} 项"

            title = titles.get(line) or f"引擎线 {line}"
            out.append(((title, tone, summary), details))

        # 不属于任何线的行（全局项）**并入第一条线的明细**。
        #
        # 原来单独出一行「其他」，简洁模式下两线 + 其他 = 3 行 ——
        # 又破坏了「每个游戏版本一行」的承诺。全局项（反解工程这类）
        # 本来就是给排查用的，挂在第一组里展开即可，不值得占一行。
        if other:
            if out:
                summary_row, details = out[0]
                merged = list(details) + list(other)
                tones = [r[1] for r in merged]
                tone = (
                    "err"
                    if "err" in tones
                    else ("warn" if "warn" in tones else "ok")
                )
                bad_names = [r[0] for r in merged if r[1] != "ok"]
                text = (
                    f"{len(merged)} 项检查全部正常"
                    if tone == "ok"
                    else f"{len(bad_names)} 项需注意：{'、'.join(bad_names[:4])}"
                )
                out[0] = ((summary_row[0], tone, text), merged)
            else:
                tones = [r[1] for r in other]
                tone = (
                    "err"
                    if "err" in tones
                    else ("warn" if "warn" in tones else "ok")
                )
                bad_names = [r[0] for r in other if r[1] != "ok"]
                text = (
                    f"{len(other)} 项检查全部正常"
                    if tone == "ok"
                    else f"{len(bad_names)} 项需注意：{'、'.join(bad_names[:4])}"
                )
                out.append((("环境", tone, text), other))

        return out

    def _toggle_details(self) -> None:
        self.show_details = not self.show_details
        self.detail_btn.setText("收起明细" if self.show_details else "展开明细")
        self.refresh()

    def refresh(self) -> None:
        # **每次刷新都重读设置**，不能只在构造时读一次。
        #
        # 原来 ``show_details`` 只在 ``__init__`` 里赋值，于是玩家在设置页
        # 勾了「详细展示」并保存、主窗口调 ``diagnostics.refresh()`` 之后，
        # 页面仍然按旧的折叠状态渲染 —— 表现就是「勾了没反应」。
        #
        # 这里用「设置值 OR 页面内的临时开关」：
        # 玩家在设置里开启 → 详细；在页头点了「展开明细」→ 也详细。
        # 两者任一为真就展开，不会被另一个覆盖掉。
        from .. import features as _features

        configured = _features.show_detail(self.cfg)
        if configured != self._last_configured_detail:
            # 设置变了：以设置为准，清掉页面内的临时覆盖
            self.show_details = configured
            self._last_configured_detail = configured
            if getattr(self, "detail_btn", None) is not None:
                self.detail_btn.setText("收起明细" if configured else "展开明细")
        rows: list[tuple[str, str, str, str]] = []

        # 引擎：每条线一行
        #
        # **显示真正会被用的那个文件**，不是配置里的 ``engines.<line>.path``。
        #
        # 原因：``runner.host_paths()`` 根本不读配置的那一项 —— 它固定取
        # ``runtime/host<tag>/jjxf<tag>.exe``（模板构建必须与同名 .pck 配对，
        # 路径不能随意指）。配置里的 path 是历史遗留字段，实测 4.7 那条
        # 指向 ``engines/godot-4.7.2.exe``，与实际使用的
        # ``runtime/host47/jjxf47.exe`` 是两份独立的同大小拷贝。
        #
        # 诊断页的职责是让人看清「到底在用什么」。显示一个不参与启动的
        # 路径，等于把排查方向引错 —— 玩家照那个路径去换文件，毫无效果。
        for line in self._lines():
            tag = line.replace(".", "")
            host_exe = paths.ROOT / "runtime" / f"host{tag}" / f"jjxf{tag}.exe"
            meta = (self.cfg.get("engines") or {}).get(line, {})
            hint = ""
            cfg_path = cfg_mod.resolve_path(str(meta.get("path") or "")) or None
            if cfg_path is not None and cfg_path.resolve() != host_exe.resolve():
                # 这句只在开发者模式显示。
                #
                # 它对玩家毫无用处 —— 玩家不会去改 engines 字段，
                # 长括号只会让一行本来就长的技术信息更难读。
                # 但对开发者是重要信息：它解释「为什么改了配置里的路径
                # 启动行为没变」，避免排查方向跑偏。
                if features.developer_only(self.cfg):
                    hint = "（配置里的 engines 路径不参与启动，实际用这个）"
            if host_exe.is_file():
                rows.append(
                    (
                        f"{line} 引擎",
                        "ok",
                        f"{host_exe}（{host_exe.stat().st_size:,} 字节）{hint}",
                    )
                )
            elif cfg_path is not None and cfg_path.is_file():
                # 运行时还没建：这时配置路径就是唯一线索，提示去重建底座
                rows.append(
                    (
                        f"{line} 引擎",
                        "warn",
                        f"运行时缺失，配置指向 {cfg_path}；点「重建兼容底座」生成",
                    )
                )
            else:
                rows.append((f"{line} 引擎", "err", f"缺失：{host_exe}"))

        # 基线：每条线的每个基线各一行
        for line, bid in self._baselines():
            meta = (self.cfg.get("baselines") or {}).get(bid, {})
            p = cfg_mod.resolve_path(str(meta.get("path") or "")) or Path("")
            if not meta.get("path") or not p.is_file():
                rows.append((f"基线 {bid}", "err", f"未导入（{line} 线）：{meta.get('path') or '未配置路径'}"))
                continue
            size = p.stat().st_size
            ok = size == meta.get("size")
            rows.append(
                (f"基线 {bid}", "ok" if ok else "warn", f"{size:,} 字节" + ("" if ok else "（与记录不符）"))
            )

        # 分层指纹：4.7 才有（4.6 的 identity 没有 code_layer）
        for line, bid in self._baselines():
            rows.extend(self._identity_row(line, bid))

        # 索引：基线 id 的后半段就是索引文件名后缀
        for _line, bid in self._baselines():
            suffix = bid.split(".v")[-1]
            idx = paths.BASELINE / f"index-{suffix}.json.gz"
            rows.append(
                (f"索引 {suffix}", "ok" if idx.exists() else "warn",
                 f"{idx.name}（{idx.stat().st_size:,} 字节）" if idx.exists() else "未生成（点「重建索引」）")
            )

        # 运行时：每条线各一行
        for line in self._lines():
            tag = line.replace(".", "")
            exe = paths.ROOT / "runtime" / f"host{tag}" / f"jjxf{tag}.exe"
            pck = paths.ROOT / "runtime" / f"host{tag}" / f"jjxf{tag}.pck"
            if exe.exists() and pck.exists():
                rows.append((f"运行时 {line}", "ok", f"{exe.name} + {pck.name}（{pck.stat().st_size:,} 字节）"))
            else:
                rows.append((f"运行时 {line}", "err", "缺 exe 或底座 pck，去“打包导出”重建"))

        reversed_project = (self.cfg.get("sources") or {}).get("reversed_project") or ""
        sp = Path(reversed_project) if reversed_project else None
        # 玩家版根本看不到「反解工程」设置行，体检表里再列它只会变成一条
        # 永远「未配置」的告警，反而让人以为坏了。
        if features.show_source_project(self.cfg):
            # **「未配置」不是问题。**
            #
            # 反解工程是开发者工具（取 .gd 原文用），普通使用完全不需要它。
            # 原来标 warn，后果和「源码漂移未检测」一样：诊断页永远挂一条
            # 黄字，两条线因此都被判定为「N 项需注意」，简洁模式的折叠全部
            # 失效 —— 面板一打开就是满屏明细，正是要避免的「玩家觉得复杂」。
            #
            # 状态记 ok（=「无需处理」），详情里说清未配置。
            if sp and sp.exists():
                rows.append(("反解工程", "ok", str(sp)))
            else:
                rows.append(
                    ("反解工程", "ok", "未配置（开发者工具，不影响游戏运行）")
                )

        from ....core import drift as drift_mod

        for line in self._lines():
            last = drift_mod.load_last(line)
            if not last:
                # **"没检测过"不是问题。**
                #
                # 原来这里标 "warn"，后果是：两条线的源码漂移都没跑过，
                # 于是整个诊断页永远显示「2 项需注意」，玩家第一眼就
                # 看到一个黄点 —— 但他什么也没做错，只是没点那个按钮。
                #
                # 状态记 "ok"（=「无需处理」），详情里说清「未检测」，
                # 想跑的人自然会看到按钮。这样「注意」才真的代表有事。
                rows.append(
                    (f"源码漂移 {line}", "ok", "未检测（可选，点下方「源码漂移检测」）")
                )
                continue
            counts = last.get("counts") or {}
            base = last.get("baseline") or {}
            src = last.get("source") or {}
            detail = (
                f"脚本 {base.get('scripts')}→{src.get('scripts')}，场景 {base.get('scenes')}→{src.get('scenes')}；"
                f"缺 {counts.get('missing_scripts', 0)} / 多 {counts.get('extra_scripts', 0)} / "
                f"钩子不通过 {counts.get('hooks_bad', 0)}（{last.get('checked_at', '')}）"
            )
            rows.append((f"源码漂移 {line}", "ok" if last.get("ok") else "err", detail))

        # ---------------------------------------------------------------
        # 扩展补丁生效情况
        #
        # 这一项存在的唯一目的，是把「已启用但没生效」这件事暴露出来。
        #
        # 玩家的实际遭遇（2026-11-08 反馈）：
        #   扩展列表显示「已启用」，启动日志也显示覆盖层挂载成功，
        #   但进游戏发现功能没有任何变化。
        #
        # 原因：overlay-47.pck 确实挂上了，但里面那条 text_patch 在合成
        # 基线内容时锚点命中 0 处（剧情修复版多了一行注释），改动被
        # compose 层丢弃 —— 文件在、补丁没了。
        #
        # 之前这个信息只写进 builds/<run>/compose.json，玩家不会去看。
        # 现在读 effect-report.json，直接在体检表里报出来。
        # ---------------------------------------------------------------
        for line in self._lines():
            report = self._last_effect_report(line)
            if not report:
                continue
            failed = report.get("failed_patches") or []
            if not failed:
                # 全部生效时**不加行**。
                #
                # 简洁模式的承诺是「每个游戏版本一行」。多一条「扩展补丁」
                # 独立行，两线就变四行，又回到「玩家觉得复杂」。
                # 只有真有问题才值得占一行 —— 那时它和版本行一样重要。
                continue
            names = sorted(
                {
                    str(f.get("ext_id") or f.get("target") or "?")
                    for f in failed
                    if isinstance(f, dict)
                }
            )
            rows.append(
                (
                    f"扩展补丁 {line}",
                    "err",
                    f"{len(failed)} 处改动未生效（{'、'.join(names[:3])}）—— "
                    "扩展显示已启用但游戏里不会有变化，请检查锚点是否按该线源码写",
                )
            )

        self.table.setRowCount(0)

        # ---------------------------------------------------------------
        # 折叠：正常时每条线只占一行，出问题才展开明细。
        #
        # 为什么这么做
        # ------------
        # 玩家眼里只有**两个游戏**：「1.45 原版」和「4.7 剧情版」。
        # 而底层每条线有 6 个部件（引擎 / 基线 / 指纹 / 索引 / 运行时 / 漂移）。
        # 两条线铺平就是 11 行，玩家看到的是「一堆看不懂的东西乘以二」，
        # 第一反应是「这软件好复杂」—— 而他根本不需要知道「索引」是什么。
        #
        # 折叠后：
        #   · 全正常  → 2 行：「机甲旋风 1.45 · 原版  正常」「机甲旋风 4.7 · 剧情版  正常」
        #   · 有问题  → 那一行标红/黄，**并自动展开**它自己的明细
        #
        # 关键在于「自动展开」：出了问题还折叠，玩家就得先猜该点哪儿。
        # 正常时收起、异常时摊开，才既省空间又不妨碍排查。
        # ---------------------------------------------------------------
        grouped = self._group_by_line(rows)
        if self.show_details:
            display = list(rows)
        else:
            display = []
            for summary, details in grouped:
                display.append(summary)
                # 这条线有问题就把它的明细摊出来
                if summary[1] != "ok":
                    display.extend(details)

        bad = 0
        for name, tone, detail in display:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            item = QTableWidgetItem({"ok": "正常", "warn": "注意", "err": "异常"}[tone])
            item.setForeground(
                QBrush(QColor(colors.ok())) if tone == "ok" else (QBrush(QColor(colors.warn())) if tone == "warn" else QBrush(QColor(colors.err())))
            )
            self.table.setItem(r, 1, item)
            self.table.setItem(r, 2, QTableWidgetItem(detail))
            if tone != "ok":
                bad += 1

        self.state_pill.setText(
            "全部正常" if bad == 0 else f"{bad} 项需注意"
        )
        self.state_pill.set_tone("ok" if bad == 0 else "warn")
        # 日志记的是**明细总数**，不是折叠后的显示行数 ——
        # 排查时要的是「一共几项、几项坏」，不是「屏幕上有几行」。
        self._log(
            f"体检完成：{len(rows)} 项，{bad} 项异常"
            + ("" if self.show_details else f"（已折叠为 {len(grouped)} 行，点「展开明细」看全部）"),
            "ok" if bad == 0 else "warn",
        )

    # ------------------------------------------------------------ 动作

    def _run(self, fn, *args) -> None:
        from ..workers import TaskWorker

        for btn in (self.verify_btn, self.base_btn, self.drift_btn, self.smoke46_btn, self.smoke47_btn):
            btn.setEnabled(False)

        worker = TaskWorker(fn, *args)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._enable_buttons)
        self._worker = worker
        worker.start()

    def _enable_buttons(self) -> None:
        for btn in (self.verify_btn, self.base_btn, self.drift_btn, self.smoke46_btn, self.smoke47_btn):
            btn.setEnabled(True)

    def _on_done(self, result) -> None:
        if isinstance(result, str):
            self._log(result, "ok")
        elif isinstance(result, dict):
            if result.get("ok") is not None:
                self._log(
                    f"冒烟结果：ok={result.get('ok')} rc={result.get('returncode')} "
                    f"{result.get('seconds')}s {result.get('smoke') or ''}",
                    "ok" if result.get("ok") else "err",
                )
                for err in result.get("errors") or []:
                    self._log(f"  {err}", "err")
                # 只有冒烟真的通过才解门禁。失败时保持 pending/failed ——
                # 「跑过就算验过」会让这道闸门形同虚设。
                if result.get("ok") and result.get("line"):
                    self._mark_line_verified(str(result["line"]))
            else:
                self._log(str(result), "ok")
        self.refresh()

    def _on_failed(self, message: str, tb: str) -> None:
        self._log(f"执行失败：{message}", "err")
        if tb:
            self._log(tb.strip().splitlines()[-1], "err")

    def _verify(self) -> None:
        self._log("开始校验基线 sha256（3 GB 级文件约数秒到十几秒）…", "info")

        def task() -> str:
            from ....core import baseline_identity as bi

            lines = []
            # 按配置遍历**所有**基线，不再只认 1.45。
            for line, bid in self._baselines():
                meta = (self.cfg.get("baselines") or {}).get(bid) or {}
                p = cfg_mod.resolve_path(str(meta.get("path") or "")) or Path("")
                if not meta.get("path") or not p.is_file():
                    lines.append(f"{bid}：未导入")
                    continue

                # 有 identity 的走**分层校验**：能说清是 body/exe/代码层哪一项不符，
                # 比「整包 sha256 不一致」有用得多。4.7 就是这条路。
                try:
                    if bi.load_identity(self.cfg, line):
                        check = bi.verify(self.cfg, line, force_hash=True)
                        if check.ok:
                            lines.append(f"{bid}：指纹一致（{'/'.join(l.name for l in check.layers)}）")
                        else:
                            bad = "、".join(l.name for l in check.failing())
                            lines.append(f"{bid}：不符（{bad}）—— {check.message[:60]}")
                        continue
                except Exception:  # noqa: BLE001 - 回落到单哈希比对
                    pass

                expect = str(meta.get("sha256") or "")
                if not expect:
                    lines.append(f"{bid}：已导入，未登记哈希（重建索引可补）")
                    continue
                actual = sha256_file(p)
                ok = actual == expect
                lines.append(f"{bid}：{'一致' if ok else '不一致'} {actual[:16]}…")
            return "；".join(lines)

        self._run(task)

    def _rebuild_base(self) -> None:
        # 跟着冒烟那边的线选择走：两条线各有自己的底座工程，
        # 写死 "46" 等于 4.7 永远没法在这里重建。
        line = str(self.smoke_line.currentData() or cfg_mod.BUILTIN_LINE)
        tag = line.replace(".", "")
        self._log(f"开始重建兼容底座（{line} 线）…", "info")

        def task() -> str:
            import subprocess
            import sys

            proc = subprocess.run(
                [sys.executable, str(paths.TOOLS / "build_base.py"), tag],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            tail = (proc.stdout or "").strip().splitlines()[-6:]
            return f"重建 {line} 底座完成（rc={proc.returncode}）：" + " | ".join(tail)

        self._run(task)

    def _drift(self) -> None:
        self._log("开始源码漂移检测：比对脚本/场景集合与 .gdc 魔数…", "info")

        def task() -> str:
            from ....core import drift as drift_mod

            parts = []
            for line in self._lines():
                report = drift_mod.analyze(line)
                counts = report.as_dict()["counts"]
                parts.append(
                    f"{line}：{'一致' if report.ok else '有漂移'}"
                    f"（脚本 {report.baseline_scripts}/{report.source_scripts}，"
                    f"缺 {counts['missing_scripts']}，多 {counts['extra_scripts']}，"
                    f"钩子不通过 {counts['hooks_bad']}）"
                )
            return "；".join(parts)

        self._run(task)

    def _smoke(self, line: str) -> None:
        self._log(f"开始冒烟 {line}（要挂载数 GB 资源包，请稍候）…", "info")

        def task() -> dict:
            cfg = cfg_mod.load()
            bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
            rd = runner.run_dir_for(f"smoke{line.replace('.', '')}")
            packs = runner.baseline_packs(cfg, bid)
            packset = runner.make_packset(
                rd / "packset.json", packset_id=f"{bid}-{line}", line=line, packs=packs, baseline_id=bid
            )
            res = runner.launch(cfg, line, packset=packset, smoke=True, headless=True, run_dir=rd)
            out = res.summary()
            # 把线带回来：``_on_done`` 要靠它把「已校验」写回配置。
            out["line"] = line
            return out

        self._run(task)

    def _mark_line_verified(self, line: str) -> None:
        """冒烟通过 → 把这条线的基线置为「已校验」。

        这是「诊断页补验」能真正解除启动门禁的关键：玩家导入时若跳过
        校验，状态是 ``pending``、按钮禁用；到这里跑一遍冒烟却不改状态，
        等于白跑，他会一直卡在「待校验」。

        只对**已有状态字段**的基线写。老配置没这个字段、本来就不拦，
        冒烟顺手给它补一个 verified 反而让「这台机器校验过」变成假事实。
        """
        from ....core import baseline_ops

        bid = cfg_mod.BASELINE_OF.get(line, cfg_mod.BUILTIN_BASELINE)
        if not baseline_ops.verify_state(self.cfg, bid):
            return
        baseline_ops.mark_verified(
            self.cfg, bid, by=baseline_ops.VERIFY_BY_SMOKE, note="诊断页冒烟"
        )
        try:
            cfg_mod.save(self.cfg)
        except OSError:
            pass
        baseline_ops.invalidate_caches()
        self._log(f"{line} 本体校验通过，已解除启动限制", "ok")
