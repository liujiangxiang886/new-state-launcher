"""界面测试的收尾工具：把测试创建的控件**真正**销毁。

为什么单独抽出来（2026-11-13 两次排查的结论）：

``deleteLater()`` 只是**排队**一个删除事件。测试里没有事件循环，没人派发
``DeferredDelete``，控件就一直活到解释器退出。这时 shiboken 再去析构对应的
C++ 对象：``Windows fatal exception: access violation``（``0xC0000005``）
或 fail-fast（``0xC0000409``）。

表现极具误导性：报告写着 ``44 passed``，进程退出码却是 ``3221225477`` ——
脚本和 CI 一律判定失败，而失败原因跟被测功能毫无关系。

用法::

    from qt_cleanup import dispose

    pages = []
    ...
    dispose(*pages)          # 在测试或 fixture 收尾里调用
"""

from __future__ import annotations


def dispose(*widgets) -> int:
    """关闭 → 解除父子关系 → 排队删除 → **立即派发**删除事件。

    返回实际处理的数量（已被 C++ 侧销毁的会跳过），便于断言。
    """
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    handled = 0
    for widget in widgets:
        if widget is None:
            continue
        try:
            widget.close()
            widget.setParent(None)
            widget.deleteLater()
            handled += 1
        except RuntimeError:
            pass                    # 底层 C++ 对象已经没了，跳过

    # 关键一步：没有事件循环时 DeferredDelete 永远不执行，必须手动派发。
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    return handled
