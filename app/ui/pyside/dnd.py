"""拖拽解析：把「一次拖进来的所有扩展包」一次取全。

为什么要单独成模块：主窗口与扩展页过去各写了一份 ``_drop_path``，都是
「遍历 url，命中第一个就 return」——这就是「拖 5 个只装 1 个」的直接原因。
两份实现还各自定义了一份 ``DROP_SUFFIXES``，改一处漏一处。

这里只做纯解析（不碰 Qt 控件），所以可以脱离窗口单测。
"""

from __future__ import annotations

from pathlib import Path

#: 可拖入/可选中的扩展包后缀
DROP_SUFFIXES = {".zip", ".jjm", ".pack"}


def _key(path: Path) -> str:
    """去重用的键。Windows 路径不区分大小写，同一个文件拖两次不该装两遍。"""
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def drop_paths(event) -> list[Path]:
    """按用户拖入的顺序返回所有本地扩展包（去重，忽略其它类型）。

    保留顺序是有意的：用户多选时往往按「先装基础包、再装扩展包」的顺序选，
    打乱会让 ``load_after`` 之外的隐式依赖（例如先装框架再装内容）失效。
    """
    mime = event.mimeData()
    if mime is None or not mime.hasUrls():
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.suffix.lower() not in DROP_SUFFIXES:
            continue
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def drop_path(event) -> Path | None:
    """兼容旧调用：只取第一个。新代码请用 :func:`drop_paths`。"""
    found = drop_paths(event)
    return found[0] if found else None
