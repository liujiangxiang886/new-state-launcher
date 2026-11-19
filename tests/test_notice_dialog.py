"""首启须知与开发包《免责声明.md》的一致性守卫。

背景：弹窗正文是 notice_dialog.py 里硬编码的 HTML，不读 md 文件；
开发包里那份免责声明是给创作者看的。2026-11-11 就出现过「3.5 内存优化 /
3.6 自定义版本接入 / 联机禁入只写进 md、没进弹窗，且 NOTICE_VERSION 没升」
的情况——老玩家根本不会重新看到新条款。

这里把两件事钉死：
1. 任何新条款必须同时出现在弹窗正文里；
2. 改文案必须升 NOTICE_VERSION，否则旧 notice_ack 不触发重新征询。
"""

from __future__ import annotations

from pathlib import Path

from app.ui.pyside import app as app_mod
from app.ui.pyside.notice_dialog import HOLD_SECONDS, NOTICE_VERSION, _BODY

ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER = ROOT / "扩展开发文档" / "公共文档" / "免责声明.md"


def test_notice_version_bumped_for_new_clauses() -> None:
    assert NOTICE_VERSION == "2"


def test_old_ack_forces_reconfirmation() -> None:
    assert not app_mod._notice_accepted({"ui": {"notice_ack": "1"}})
    assert not app_mod._notice_accepted({})
    assert app_mod._notice_accepted({"ui": {"notice_ack": NOTICE_VERSION}})


def test_new_clauses_are_in_popup() -> None:
    for keyword in (
        "自定义版本",
        "改版创作者",
        "不逆向",
        "反解源码",
        "不构成无恶意代码",
        "P2P",
        "系统级操作",
        "默认关闭",
        "不降低游戏本身的内存占用",
    ):
        assert keyword in _BODY, f"弹窗漏了新条款关键词：{keyword}"


def test_disclaimer_doc_clauses_exist() -> None:
    text = DISCLAIMER.read_text(encoding="utf-8")
    for section in (
        "3.5 系统内存优化",
        "3.6 自定义版本接入",
        "不支持联机接入",
        "反解工程",
    ):
        assert section in text, f"开发包免责声明缺条款：{section}"


def test_doc_and_popup_stay_in_sync() -> None:
    """md 里每条新增条款，弹窗都要有对应表达。"""
    text = DISCLAIMER.read_text(encoding="utf-8")
    pairs = (
        ("3.5 系统内存优化", "默认关闭"),
        ("3.6 自定义版本接入", "改版创作者"),
        ("不支持联机接入", "P2P"),
    )
    for doc_marker, popup_keyword in pairs:
        assert doc_marker in text
        assert popup_keyword in _BODY, (
            f"开发包有「{doc_marker}」，弹窗却没有「{popup_keyword}」"
        )


def test_hold_seconds_unchanged() -> None:
    """强制阅读时长是产品明确指定的 15 秒，别被文案长度顺手改掉。"""
    assert HOLD_SECONDS == 15
