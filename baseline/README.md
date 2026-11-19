# baseline/ 说明

本目录只公开**整包指纹**，不公开任何反解产物。

## 入库的文件

| 文件 | 内容 |
|---|---|
| `identity-1.45.json` | 4.6 线（原版 1.45）本体的结构指纹 |
| `identity-4.7.json` | 4.7 线（剧情版）本体的结构指纹 |

指纹字段都是**统计量**，不含游戏内容：

- `size`、`sha256`（整包哈希，空值表示未记录）
- `header`：Godot pck 头（版本、引擎线、目录偏移、pck 起始与长度）
- `entry_count`、`total_entry_bytes`、`encrypted_entries`
- `top_suffixes`：条目后缀分布
- `layers` / `code_layer`：代码层文件数与哈希（4.7）

`path` 字段出厂为空。它只记录采集时本体的位置，不参与匹配；留空是为了
不把采集者的本机目录带进仓库。

## 不入库的文件

| 文件 | 原因 |
|---|---|
| `index-*.json.gz`（各约 14.5 MB） | 反解工程的**路径索引**，属游戏本体的衍生数据 |
| `sources-*.zip` / `sources-*.json` | 反解源码及其索引 |
| `canonical_sources.zip` / `.json` | GDRE 导出的完整工程 |

这些文件由使用者用自备的本体在本地生成，不随仓库分发。缺少索引时
`app/core/baseline.py::load_index()` 会抛 `FileNotFoundError`，并提示
先运行 `python tools/make_index.py`。

## 为什么指纹可以公开

指纹只描述「这个文件是不是预期的那个包」：大小、pck 头、条目数量与后缀分布。
它不包含脚本、场景、贴图、对话或任何可还原内容的字节。玩家导入本体后，
启动器用这些统计量判断版本是否匹配，从而决定能否使用对应线的扩展。
