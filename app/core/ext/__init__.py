"""扩展/剧情协议：清单解析、升格、校验、安装服务。

注意：这里**不要**再 re-export ``service`` 这个函数。它会把子模块
``app.core.ext.service`` 的同名属性遮蔽掉，导致
``import app.core.ext.service as m`` 拿到的是函数而不是模块
（测试里 monkeypatch 模块常量时会直接报 AttributeError）。全项目
没有 ``from app.core.ext import service`` 的用法，删掉不影响调用方。
"""

from .manifest import Manifest, load, normalize, read_manifest
from .service import ExtensionService, InstallResult

__all__ = [
    "Manifest",
    "load",
    "normalize",
    "read_manifest",
    "ExtensionService",
    "InstallResult",
]
