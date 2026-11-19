"""导入时的钩子提示可见性（产品约定，不是实现细节）。

两档制的约定：

- **完全校验**：始终提。作者/玩家都需要知道哪些钩子没落到源码上 ——
  这一档的承诺就是「装上的都是验证过的」，那就必须把「哪些没落上」说清。
- **宽松校验**：单个扩展不提，导入整合包（合集）时才提。
  装单个包时玩家不想看实现细节；装整合包时他才需要知道哪几个没生效。

（旧的三档制里还有一个「普通模式」，完全不提钩子。那个档已经删了 ——
它什么都不查，和宽松档合并后，「提不提」就只剩合集这一个维度。）
"""

from __future__ import annotations

import pytest

from app.core.ext.service import _show_hook_info


@pytest.mark.parametrize(
    ("mode", "in_bundle", "expected"),
    [
        ("loose", False, False),
        ("loose", True, True),
        ("strict", False, True),
        ("strict", True, True),
    ],
)
def test_show_hook_info(mode: str, in_bundle: bool, expected: bool) -> None:
    assert _show_hook_info(mode, in_bundle) is expected


@pytest.mark.parametrize("legacy", ["normal", "strict_warn"])
def test_legacy_loose_names_behave_like_loose(legacy: str) -> None:
    """旧配置里残留的宽松档名，行为要与新名一致。"""
    assert _show_hook_info(legacy, False) is False
    assert _show_hook_info(legacy, True) is True


@pytest.mark.parametrize("legacy", ["strict_verify", "developer"])
def test_legacy_strict_names_behave_like_strict(legacy: str) -> None:
    assert _show_hook_info(legacy, False) is True
    assert _show_hook_info(legacy, True) is True


def test_unknown_mode_falls_back_to_loose() -> None:
    """配置里写错一个词不该导致安装流程抛异常。

    回落宽松档：单包不提、合集提 —— 与「玩家手改配置写错」的实际处境
    相符（他不想要技术细节，但装整合包时仍该知道哪些没生效）。
    """
    assert _show_hook_info("not-a-mode", False) is False
    assert _show_hook_info("not-a-mode", True) is True