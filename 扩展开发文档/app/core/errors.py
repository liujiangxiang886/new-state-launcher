"""最小异常定义（发布包精简版）。

启动器完整版的 ``app/core/errors.py`` 有几十个异常类型；发布包里只有
pck 读取路径，用到的就这 4 个。保留同样的类名与 ``code`` 属性，
方便报错信息与原版对齐。
"""


class PckError(Exception):
    """pck 相关错误的基类。"""

    code = "E_PCK"


class PckFormatError(PckError):
    """pck 头部或目录结构不合法。"""

    code = "E_PCK_FORMAT"


class PckIntegrityError(PckError):
    """pck 数据校验失败（大小或 md5 不符）。"""

    code = "E_PCK_INTEGRITY"


class PckEncryptedError(PckError):
    """目录区或条目被加密，读不了。"""

    code = "E_PCK_ENCRYPTED"


class PckNotFoundError(PckError):
    """文件里找不到 PCK（exe 未内嵌、或扩展名不对）。"""

    code = "E_PCK_NOT_FOUND"
