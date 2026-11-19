"""PCK 读取（发布包里的精简版）。

完整版还包含分片打包（``packset``）与写入（``writer``）；发布包只需要
读能力，所以这里只导出 ``PckReader`` 相关接口，避免带入多余依赖。
"""

from .reader import PckEntry, PckHeader, PckReader, locate_pck

__all__ = [
    "PckEntry",
    "PckHeader",
    "PckReader",
    "locate_pck",
]