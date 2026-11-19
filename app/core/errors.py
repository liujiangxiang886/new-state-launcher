"""统一错误类型与错误码。

设计原则：
- 每个异常带稳定的 ``code``（用于 UI 展示与日志检索，不随文案变化）；
- ``detail`` 保存结构化上下文，便于诊断包导出；
- 面向用户的消息简洁，技术细节留在 ``detail`` 与日志里。
"""

from __future__ import annotations

from typing import Any


class LauncherError(Exception):
    """所有可预期错误的基类。"""

    code: str = "E_UNKNOWN"
    http_like: int = 500

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - 展示用
        if not self.detail:
            return f"[{self.code}] {self.message}"
        extra = ", ".join(f"{k}={v!r}" for k, v in self.detail.items())
        return f"[{self.code}] {self.message} ({extra})"


# ---------------------------------------------------------------- PCK 相关


class PckError(LauncherError):
    code = "E_PCK"


class PckFormatError(PckError):
    """头部/目录区结构不符合预期。"""

    code = "E_PCK_FORMAT"


class PckEncryptedError(PckError):
    """目录区或条目被加密，读不了。"""

    code = "E_PCK_ENCRYPTED"


class PckNotFoundError(PckError):
    """文件里找不到 PCK（exe 未内嵌、或扩展名不对）。"""

    code = "E_PCK_NOT_FOUND"


class PckIntegrityError(PckError):
    """md5/大小校验失败。"""

    code = "E_PCK_INTEGRITY"


# ---------------------------------------------------------------- 基线与引擎


class BaselineError(LauncherError):
    code = "E_BASELINE"


class BaselineMismatch(BaselineError):
    """基线指纹不匹配。``level`` 取 ``warn`` / ``block``。"""

    code = "E_BASELINE_MISMATCH"

    def __init__(self, message: str, level: str = "warn", **detail: Any) -> None:
        super().__init__(message, **detail)
        self.level = level


class EngineError(LauncherError):
    code = "E_ENGINE"


class EngineMissing(EngineError):
    code = "E_ENGINE_MISSING"


class PackVersionMismatch(EngineError):
    """分片的 pack_version 高于该引擎线能读的上限。

    实测：新版引擎能读 v3 与 v4，4.6 只到 v3（喂 v4 会 ``Pack version unsupported``）。
    属于「启动前就该拒」的错误，别让它变成一句看不懂的引擎报错。
    """

    code = "E_PACK_VERSION"


# ---------------------------------------------------------------- 扩展与构建


class ManifestError(LauncherError):
    """扩展/剧情清单不合法。"""

    code = "E_MANIFEST"


class ResolveError(LauncherError):
    """依赖、冲突、环检测失败。"""

    code = "E_RESOLVE"


class HookUnsafeError(LauncherError):
    """方法钩子无法安全改写目标函数体。"""

    code = "E_HOOK_UNSAFE"


class BuildError(LauncherError):
    code = "E_BUILD"


class SmokeError(LauncherError):
    """冒烟测试未通过。"""

    code = "E_SMOKE"


# ---------------------------------------------------------------- 面向用户的提示

#: 出错时统一附在末尾的提示。
#:
#: 为什么要做成常量：安装失败、启动被拒、CLI 报错三处都要说同一件事，
#: 各写一份迟早漂移成三种说法；而且这句话必须**明确指向日志**——用户
#: 拍屏幕发过来，我们拿到的是被截断的报错和丢失的堆栈，只能靠猜。
LOG_HINT = (
    "请点「复制日志」把完整日志发给 AI 排查，或先「保存日志」再发文件。\n"
    "不要用手机拍屏幕——照片会截断报错、丢掉堆栈，别人只能靠猜。"
)
