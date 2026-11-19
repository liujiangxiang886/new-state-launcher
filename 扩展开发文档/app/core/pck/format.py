"""PCK 二进制格式常量与结构体定义。

格式来源：对 1.45（Godot 4.6.0，pack_version=3）与新版引擎（pack_version=4，
pack_version=4）两个真实基线的逐字节实测，而非文档推测。实测结论：

    头部（v3/v4 一致，共 40 字节有效，填充至 file_base=112）
      0   "GDPC"
      4   u32 pack_version
      8   u32 ver_major
      12  u32 ver_minor
      16  u32 ver_patch
      20  u32 pack_flags
      24  u64 file_base       数据区相对 pck 起点的偏移
      32  u64 dir_offset      目录区相对 pck 起点的偏移
      40  ...                 保留（全 0，到 file_base 为止）

    目录区（pck_start + dir_offset）
      u32 file_count
      重复 file_count 次：
        u32  path_len（已含 \\0 填充）
        u8[] path（UTF-8，尾部以 \\0 补齐到 path_len）
        u64  offset（相对数据区起点，即 pck_start + file_base）
        u64  size
        u8[16] md5
        u32  entry_flags

    条目数据位置 = pck_start + file_base + offset

注：``pack_flags`` 的 bit0 是目录加密、bit1 是相对基址标志。两个基线都是
``pack_flags=2``，表示 file_base 生效——早期把 bit1 误读成「目录加密」是常见坑。
"""

from __future__ import annotations

import struct

MAGIC = b"GDPC"

# 头部长度（v3/v4）
HEADER_SIZE = 112

# 包级标志
PACK_DIR_ENCRYPTED = 1 << 0
PACK_REL_FILEBASE = 1 << 1

# 条目级标志
FILE_ENCRYPTED = 1 << 0

#: 引擎线 -> pack_version。内置线 4.6 写 3。
#:
#: **v4 格式本身仍要支持读取**：玩家的自定义改版可能就是 v4 的包，
#: 读不出来等于「导入即失败」。所以写侧只允许内置线，
#: 读侧仍认 v3 / v4 两种（见 :data:`SUPPORTED_PACK_VERSIONS`）。
#: 引擎线 → 写侧 pack_version。4.6 写 v3，4.7 写 v4。
PACK_VERSION_BY_ENGINE: dict[str, int] = {"4.6": 3, "4.7": 4}

#: 能读的 pack_version。写只支持 :data:`PACK_VERSION_BY_ENGINE` 里的值。
SUPPORTED_PACK_VERSIONS: tuple[int, ...] = (3, 4)

#: 反向映射，用于把未知包归类到引擎线。
ENGINE_BY_PACK_VERSION: dict[int, str] = {3: "4.6", 4: "4.7"}

#: 目录区条目结构（不含变长路径）
ENTRY_FIXED = struct.Struct("<QQ16sI")   # offset, size, md5, flags
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")

#: 内嵌包尾部标记长度：u64 pck_size + b"GDPC"
EMBED_TAIL_SIZE = 12


def pack_version_for(line: str) -> int:
    try:
        return PACK_VERSION_BY_ENGINE[line]
    except KeyError as exc:
        raise ValueError(f"未知引擎线：{line!r}（写侧支持 {'、'.join(PACK_VERSION_BY_ENGINE)}）") from exc


def engine_line_for(pack_version: int) -> str | None:
    return ENGINE_BY_PACK_VERSION.get(pack_version)
