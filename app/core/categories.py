"""游戏分类：把游戏库里「能启动的游戏」按玩家自己的口径分组。

为什么单独一个模块
------------------

分类是**用户数据**，不是界面状态：

- 玩家要能自己增删改名（「我这个是魔改版」「那个是联机版」）；
- 分类要能存进配置跟着目录一起搬走；
- 条目被删时它的分类归属要能回落，不能留下指向空分类的悬空记录。

塞进 ``library.py`` 会让界面代码持有跨页面数据；塞进 ``catalog.py`` 又会让
「读投影」和「写用户数据」混在一起（catalog 明确只做只读投影）。所以独立。

作用范围
--------

只有**游戏条目**（内置基线 + 自定义线）有分类。剧情包和扩展不是「一个游戏」，
它们挂在某条引擎线上，分类对它们没有意义——``catalog`` 里这两个 kind 的
``category`` 恒为空，界面按 kind 分组显示，不参与分类筛选。

数据形态
--------

``config/launcher.json`` 的 ``categories`` 数组：

    [{"id": "official", "name": "原版", "order": 0, "color": ""}, ...]

条目侧只存 **id**（``cfg["baselines"][bid]["category"]`` 与
``CustomLine.category``）。存 id 而不是名字：改名不该让所有条目失联。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

#: 内置分类 id —— 这几个不能被删（``other`` 是删除其它分类后的回落目标）
OFFICIAL_ID = "official"
STORY_ID = "story"
MOD_ID = "mod"
OTHER_ID = "other"

#: 内置分类。玩家可以改名字，但 id 与存在性保持稳定。
DEFAULTS: tuple[dict, ...] = (
    {"id": OFFICIAL_ID, "name": "原版", "order": 0, "color": ""},
    {"id": STORY_ID, "name": "剧情", "order": 10, "color": ""},
    {"id": MOD_ID, "name": "改版", "order": 20, "color": ""},
    {"id": OTHER_ID, "name": "其他", "order": 90, "color": ""},
)

#: 不可删除的分类
PROTECTED = (OTHER_ID,)

#: 分类名长度上限（界面是单行胶囊，太长会撑坏布局）
MAX_NAME = 16

_R_ID_OK = re.compile(r"^[a-z0-9_]{1,32}$")


@dataclass
class Category:
    id: str
    name: str
    order: int = 0
    color: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "order": self.order, "color": self.color}

    @classmethod
    def from_dict(cls, data: dict) -> "Category":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            order=int(data.get("order") or 0),
            color=str(data.get("color") or ""),
        )


def _defaults() -> list[Category]:
    return [Category.from_dict(item) for item in DEFAULTS]


def load(cfg: dict) -> list[Category]:
    """读出全部分类（按 order 升序）。

    配置里没有时返回内置默认（**不写盘** —— 只读操作不该产生副作用）。
    已有配置时补齐缺失的内置分类：删掉 ``other`` 会让删除其它分类时无处回落。
    """
    raw = cfg.get("categories")
    if not isinstance(raw, list) or not raw:
        return _defaults()

    out: list[Category] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        cat = Category.from_dict(item)
        if not _R_ID_OK.match(cat.id) or cat.id in seen or not cat.name.strip():
            continue
        cat.name = cat.name.strip()[:MAX_NAME]
        seen.add(cat.id)
        out.append(cat)

    for builtin in _defaults():
        if builtin.id not in seen:
            out.append(builtin)

    out.sort(key=lambda c: (c.order, c.id))
    return out


def save(cfg: dict, cats: list[Category]) -> None:
    """写入配置。**列表顺序即显示顺序**，这里只把 ``order`` 重排成 0,10,20…

    早期版本在这里又按 ``order`` 排了一次，结果 ``move()`` 交换完列表、
    却被排序按旧 order 还原回去，表现为「点上移没反应」。顺序由调用方
    决定，这里只负责编号。
    """
    for index, cat in enumerate(cats):
        cat.order = index * 10
    cfg["categories"] = [c.as_dict() for c in cats]


def name_of(cfg: dict, category_id: str) -> str:
    for cat in load(cfg):
        if cat.id == category_id:
            return cat.name
    return "未分类" if not category_id else category_id


def resolve(cfg: dict, category_id: str) -> str:
    """把可能失效的 id 收敛成当前存在的 id（失效 → ``other``）。"""
    text = str(category_id or "").strip()
    ids = {c.id for c in load(cfg)}
    if text in ids:
        return text
    return OTHER_ID if OTHER_ID in ids else (next(iter(ids)) if ids else "")


def default_for_baseline(baseline_id: str, line: str) -> str:
    """内置基线没填分类时的默认归属。

    只有**原版 1.45 本体**归「原版」；其它登记进来的本体归「改版」。

    判据是基线 id，不看引擎线：同一个引擎线上可以挂多个本体登记，
    按 ``line == "4.6"`` 判断会把它们全算成「原版」，分类就失去意义了。
    """
    if str(baseline_id).endswith("v1.45"):
        return OFFICIAL_ID
    return MOD_ID


def _check_name(cats: list[Category], name: str, *, exclude: str = "") -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("分类名不能为空")
    if len(text) > MAX_NAME:
        raise ValueError(f"分类名最多 {MAX_NAME} 个字")
    low = text.lower()
    for cat in cats:
        if cat.id != exclude and cat.name.strip().lower() == low:
            raise ValueError(f"已经有同名分类：{cat.name}")
    return text


def _next_order(cats: list[Category]) -> int:
    return max((c.order for c in cats), default=0) + 10


def add(cfg: dict, name: str) -> Category:
    """新增一个分类，排在最后。"""
    cats = load(cfg)
    text = _check_name(cats, name)
    cat = Category(id=f"cat_{uuid.uuid4().hex[:8]}", name=text, order=_next_order(cats))
    cats.append(cat)
    save(cfg, cats)
    return cat


def rename(cfg: dict, category_id: str, name: str) -> bool:
    cats = load(cfg)
    text = _check_name(cats, name, exclude=category_id)
    hit = False
    for cat in cats:
        if cat.id == category_id:
            cat.name = text
            hit = True
            break
    if hit:
        save(cfg, cats)
    return hit


def move(cfg: dict, category_id: str, delta: int) -> bool:
    """在排序里前移/后移一位。"""
    cats = load(cfg)
    index = next((i for i, c in enumerate(cats) if c.id == category_id), -1)
    if index < 0:
        return False
    target = index + (1 if delta > 0 else -1)
    if target < 0 or target >= len(cats):
        return False
    cats[index], cats[target] = cats[target], cats[index]
    save(cfg, cats)
    return True


def remove(cfg: dict, category_id: str) -> bool:
    """删除分类，并把归在它下面的条目**回落**到「其他」。

    回落是必须的：不处理的话条目会指向一个不存在的分类，界面上表现为
    「游戏不见了」——比直接留在「其他」里糟得多。
    """
    if category_id in PROTECTED:
        raise ValueError("「其他」是删除其它分类后的回落目标，不能删")
    cats = load(cfg)
    kept = [c for c in cats if c.id != category_id]
    if len(kept) == len(cats):
        return False
    save(cfg, kept)
    reassign(cfg, category_id, OTHER_ID)
    return True


def reassign(cfg: dict, from_id: str, to_id: str) -> int:
    """把所有指向 ``from_id`` 的条目改到 ``to_id``，返回改动条数。"""
    moved = 0
    for meta in (cfg.get("baselines") or {}).values():
        if isinstance(meta, dict) and str(meta.get("category") or "") == from_id:
            meta["category"] = to_id
            moved += 1
    for item in cfg.get("custom_lines") or []:
        if isinstance(item, dict) and str(item.get("category") or "") == from_id:
            item["category"] = to_id
            moved += 1
    return moved


def category_of_baseline(cfg: dict, baseline_id: str) -> str:
    meta = (cfg.get("baselines") or {}).get(baseline_id) or {}
    raw = str(meta.get("category") or "").strip()
    if raw:
        return resolve(cfg, raw)
    return resolve(cfg, default_for_baseline(baseline_id, str(meta.get("engine") or "")))


def set_baseline_category(cfg: dict, baseline_id: str, category_id: str) -> bool:
    meta = (cfg.get("baselines") or {}).get(baseline_id)
    if not isinstance(meta, dict):
        return False
    meta["category"] = resolve(cfg, category_id)
    return True


def sorted_ids(cfg: dict) -> list[str]:
    return [c.id for c in load(cfg)]
