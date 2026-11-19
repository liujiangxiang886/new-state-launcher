# 自定义 NPC 接入规范（数据注册型 · v1）

> **适用**：机甲旋风兼容启动器 · 目标把「我们工作室的 NPC」接入游戏
> **范围**：v1 = **只注册数据**（写进 `data/npcs.json` + 帧图就位），**不强行把 NPC 摆进场景**
> **状态**：脚手架与占位帧已就绪（`_custom_lab/npc_our_studio/`），待美术立绘替换后即可出包
> **配套文档**：`扩展打包与发布手册.md`（通用 mod 规范 / schema 4 / 错误码）、
> `4.6-mod开发规范.md` 与 `4.7-mod开发规范.md`（两条线各自的基线事实）、
> `自定义基线接入手册.md`（把改版登记进启动器，本任务不涉及）

---

## 1. 背景：参考包是什么、NPC 怎么被渲染

### 1.1 那份「NPC 资源参考」不是新 NPC，是风格底本

`NPC资源参考_20261111.zip` 是游戏原版 **104 个 NPC** 的抽取库（README 明写
「只读原游戏资源……未添加自定义 NPC」）。它给美术照着画提供：

- `assets/npcs/npcXXXXX/`：场景动作原尺寸透明 PNG（待机/闲暇）
- `previews/animations/`：按原 ticks/fps/offset 重建的循环 WebP 预览
- `metadata/npcs.json`：★ **这就是游戏的 `data/npcs.json`**（export_manifest 已核对
  4.6/4.7 两版 `data/npcs.json` 元数据完全一致）——做 mod 的权威基底
- `metadata/npcs/10001.json` 等：逐 NPC 原始元数据（帧路径、每帧时长、偏移、分层、文字）
- `metadata/dialogs.json` / `portraits.json` / `verified_names.json`

### 1.2 NPC 是「数据驱动」的（好消息）

反解源码实锤（4.6 线随包 `baseline/sources-4.6.zip`，4.7 线随包
`baseline/sources-4.7.zip`；启动器会**自动**按当前线选一份，不需要手动指定）：

- `scripts/game.gd` 启动读 `npcs = JSON.parse_string(FileAccess.get_file_as_string("res://data/npcs.json"))`
  —— 单一加载点，全局 `npcs` 字典。
- `scripts/npc.gd`（102 行）按数据渲染：取 `info.dialogues`/`info.bubbles`、用
  `TextureCache.fetch(frame_data.texture)` 播帧、点击走 `openNpcDialog`。
- 结论：**只要在 `data/npcs.json` 里加一条我们的 NPC、并把帧图放到
  `assets/npcs/npc90001/`，游戏就能渲染它**，不需要改 NPC 逻辑本身。

### 1.3 唯一的不确定点：摆进场景

数据注册容易，但要让 NPC **真正站在地图坐标上**，得有东西在场景里实例化
`npc.gd` 节点。游戏的刷怪/场景放置逻辑在 4.6 重写时部分源码缺失，这是
工程上唯一需要后续补源码或换场景 pck 的点。**v1 不做**，详见第 7 节。

---

## 2. NPC 数据结构（写 `data/npcs.json` 的字段）

取自 `metadata/npcs/10001.json`，这是游戏运行时实际消费的字段：

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `id` | int | NPC 编号，**避免与官方 10001–10079 / 30001–30027 撞** | `90001` |
| `name` | str | 显示名 | `"新起点向导"` |
| `asset` | str | 资源标识（原版是 `.swf`，自定义写 NPC 目录名即可） | `"npc90001"` |
| `age` | int | 年龄（多数 0） | `0` |
| `rank` | str | 身份/头衔 | `"新起点启动器·引导员"` |
| `from` | str | 出处 | `"新起点工作室"` |
| `description` | str | 描述（对话缺省时显示） | `"…引导 NPC…"` |
| `num_talk` | int | 对话组数 | `2` |
| `click_command` | str | 点击指令 | `"openNpcDialog"` |
| `bubbles` | str[] | 头顶随机气泡 | `["欢迎来到新起点！", "…？"]` |
| `dialogues` | str[] | 对话文本（`%C名C%` 是着色标记） | `["这里是…", "…"]` |
| `options` | list | 对话选项（空即可） | `[]` |
| `jump` | int[] | 跳转索引 | `[0, 0]` |
| `fps` | int | 帧率 | `30` |
| `container_fps` | float | 容器帧率 | `1.0` |
| `layers` | list | 图层（通常 1 层，`main:true`） | 见下 |
| `source` / `sha256` | str | 来源标记（自定义填工作室标识） | `"studio:newstart"` |

`layers[].motions` 是核心动画结构：

```
"layers": [{
  "id": 0, "main": true,
  "motions": {
    "0": { "name": "待机动作", "frames": [ {texture, ticks, offset:[x,y], alpha, rotation}, ... ] },
    "1": { "name": "闲暇动作", "frames": [ ... ] }
  }
}]
```

每帧字段：

| 字段 | 说明 |
|---|---|
| `texture` | 帧图在 pck 内的路径，如 `assets/npcs/npc90001/1_1.png` |
| `ticks` | 该帧停留时长（配合 `fps`，10≈0.33s） |
| `offset` | 局部原点相对**脚底中心**的偏移（见第 4 节算法） |
| `alpha` | 透明度（1.0） |
| `rotation` | 旋转（0.0） |

---

## 3. mod 打包格式（对齐《扩展打包与发布手册》）

安装目录里真实 mod 的形态是三件套（以「六联浮游炮」为例）：

```
packs/jjxf.floating_cannons/
├── manifest-47.v2.json     # 清单（schema 3）
├── overlay-47.pck          # 覆盖层包（GDPC 头）
└── verification.json        # 校验记录（运行期 install 要求随包搬运，见规范 P14）
```

**本 NPC mod 的运行时清单**（`_custom_lab/npc_our_studio/out/manifest-46.v2.json`）：

```json
{
  "schema": 3,
  "id": "studio.our_npc",
  "name": "新起点引导NPC",
  "version": "0.1.0",
  "kind": "extension",
  "author": "新起点工作室",
  "license": "",
  "framework": ">=0.1.5,<0.2.0",
  "priority": 50,
  "requires": [], "conflicts": [], "load_after": [], "load_before": [],
  "claims": ["npc:90001"],
  "capabilities": [],
  "files": [
    { "source": "payload/data/npcs.json",        "target": "data/npcs.json" },
    { "source": "payload/assets/npcs/npc90001/1_1.png", "target": "assets/npcs/npc90001/1_1.png" },
    "…（8 张帧图）"
  ],
  "method_hooks": []
}
```

引擎线由**文件名**表达（`manifest-46` / `overlay-46`），清单内无需再写 `engine.line`。
文件名标签由 `paths.line_tag()` 生成：`4.6` → `46`，`4.7` → `47`，自定义线
`custom:<uuid>` → `custom_<uuid>`（冒号在 Windows 文件名里非法）。**两条线的覆盖层、
清单、启用位完全独立**，同一个 id 可以同时装在 4.6 与 4.7 上，互不影响。

> **schema 4 提示（1.7 起）**：上面的示例是 schema 3，仍然读得进去。但 1.7 新增了
> schema 4 = schema 3 + **强制 `sources`**。本 NPC mod 只用 `files[]` 做整文件覆盖，
> 不写 `text_patches` / `method_hooks`，因此**不需要** `sources`；
> 一旦你要加文本补丁或方法钩子，就必须声明用的是哪份反解源码，否则装不上
> （详见《扩展打包与发布手册》§3 与 §6 的 `SRC-*` 码表）。

### 3.1 整文件覆盖 vs 数据补丁（重要取舍）

- **v1 采用整文件覆盖**：把「参考包 `metadata/npcs.json`（= 游戏 `data/npcs.json`）+
  我们的 90001 条目」整体作为 `files[]` 的 `data/npcs.json` 目标。已用 `PckReader`
  回读验证：9 条目、90001 已合入、8 张 PNG 合法。**好处是直接可跑、易验证。**
- **代价**：两个 mod 都整文件覆盖 `data/npcs.json` 会冲突（规范 P1/P3 提示
  `conflict.files` 高发）。若将来要与别的 NPC/数据 mod 共存，应改用 `data_patches`：

```json
"data_patches": [{
  "file": "data/npcs.json",
  "operations": [ { "op": "add", "path": "/90001", "value": <我们的 NPC 对象>,
                    "expect": {"exists": false}, "on_miss": "abort" } ]
}]
```

  `data_patches` 由启动器 `build_overlay` 对基线 `data/npcs.json` 做定点合并，
  `overrides` 三条件只在这一层判（规范 §2.3）。v2 再做这件事，v1 先用整文件。

---

## 4. 资源布局与美术交付规范

帧图目录：`payload/assets/npcs/npc90001/`，命名 `1_1.png…1_N.png`（待机）、
`2_1.png…2_M.png`（闲暇）。完整规范见脚手架 `art_spec.md`，摘要：

- **透明 PNG**，背景 alpha=0，单帧画布尺寸全帧一致（如 256×320）。
- 角色脚底中心对齐画布水平中点、贴画布底部。
- `offset` 算法（与参考包 `npcs/10001.json` 一致）：`offset_x = -宽/2`，`offset_y = -高`。
  256×320 → `[-128, -320]`。脚本 `--auto-offset` 可按真实 PNG 画布自动回填。
- `ticks` 默认 10（末帧 8），`fps=30`。
- 导出 100% 原尺寸，勿转 webp/jpg（游戏按 PNG 路径加载）。

---

## 5. 两条线差异（1.7 现状）

两条线都是**正式线**，各自独立安装、独立启停、独立卸载。同一个 NPC mod 要做双线，
就打两份产物、放在同一个 zip 里分别装到对应线。

| 项 | 4.6（1.45 原版） | 4.7（剧情版） |
|---|---|---|
| 线状态（1.7） | **正式线**，完全支持 | **正式线**，完全支持 |
| 基底 `data/npcs.json` | 参考包 `metadata/npcs.json` 直接可用（已核对两版一致） | 同左；脚手架也可 `--base-47` 指定从 4.7 取的版本 |
| 覆盖层/清单文件名 | `overlay-46.pck` / `manifest-46.v2.json` | `overlay-47.pck` / `manifest-47.v2.json` |
| 随包反解源码 | `baseline/sources-4.6.zip`（启动器自动使用） | `baseline/sources-4.7.zip`（启动器自动使用） |
| 代码层指纹（`code_layer`） | **无** → 不做验证标志判定（装了就参与） | **有**（`baseline/identity-4.7.json`） |
| 验证记录 `verification.json` | 不需要 | **严格档下必需**；缺失会被剔除（`verify.absent`） |
| 出厂校验档 | 都是同一份程序；**玩家版出厂 `loose`，开发者版出厂 `strict`** | 同左 |
| 打包工具 | `扩展开发文档/tools/jjm_pack.py`（随开发包发，解压即用） | 同左 |

**关于 `verification.json` 的准确说法（此前文档写错过，以这里为准）**：

- 它绑的是 **4.7 代码层指纹**，所以只有 4.7 线会判；4.6 线没有指纹，
  `verify_mark` 会记一条 `verify.unavailable` 并**放行**，不会拦任何包。
- 判定只在 **`strict`（完全校验）档**生效。玩家版出厂是 `loose`，不拦；
  开发者在设置里切到「完全校验」后，未 seal 的 4.7 包会被剔除。
- 症状对照：扩展页显示「已就绪」、进游戏毫无变化、日志里有 `verify.absent` /
  `verify.stale` → 就是这条。用 `jjm_pack.py seal` 重新出记录即可。
- `jjm_pack.py` **在发布包的 `扩展开发文档/tools/` 里**（1.6 之后随包分发），
  不需要另外找作者工具包。

> 结论：4.7 可以直接做，只是**必须 seal**；4.6 不需要 seal。


---

## 6. 构建脚本用法（脚手架 `_custom_lab/npc_our_studio/`）

```
npc_our_studio/
├── npc_definition.json   # ★ 改这里即可：NPC 名称/身份/对话/帧路径/offset
├── art_spec.md           # 美术交付规范
├── build_npc_mod.py      # 构建脚本（复用 app.core.pck.writer.PckWriter）
├── payload/              # 构建中生成（data/npcs.json + 帧图）
└── out/                 # 产物：manifest-46.v2.json / overlay-46.pck / verification.json
```

**美术未出图（占位先跑通）：**
```bash
python build_npc_mod.py --ref-zip "D:/Tencent Files/NPC资源参考_20261111.zip" --line 4.6 --placeholder
```

**美术出图后（把 PNG 放 `payload/assets/npcs/npc90001/`，按 art_spec 命名）：**
```bash
python build_npc_mod.py --ref-zip "..." --line 4.6 --auto-offset
```

**4.7（需先备好 4.7 的基底 `npcs.json`）：**
```bash
python build_npc_mod.py --ref-zip "..." --base-47 "path/to/4.7/npcs.json" --line 4.7 --placeholder
# 之后用开发包自带工具补验证记录（4.7 严格档必需）：
python ../../tools/jjm_pack.py seal out/ --line 4.7
```

脚本做的事：① 读基底 `npcs.json`；② 把 `npc_definition.json` 合入（id 冲突会断言）；
③ 生成/采用帧图；④ `PckWriter(line=...)` 打包 `overlay-Xy.pck`（含 `data/npcs.json` + 帧图）；
⑤ 写出 `manifest-Xy.v2.json` + `verification.json`。

**已验证**：`PckReader` 回读 `overlay-46.pck` → 9 条目、`data/npcs.json` 含 `90001`、8 张 PNG 头合法。

---

## 7. 安装与验证（启动器）

经启动器「扩展」页导入 `out/` 下的 `manifest-46.v2.json` + `overlay-46.pck`
（或打成 zip 走 `cli mods install`）。验证清单：

- [ ] 扩展页显示「已启用 / 已就绪」，无 `verify.absent` 报错
- [ ] 启动游戏，`data/npcs.json` 被覆盖层替换（含 90001 条目）
- [ ] NPC 可被对话/传送系统引用（v1 不保证自动出现在地图，见下）
- [ ] 日志无 `TextureCache` 找不到纹理的报错

> v1 数据注册已足够证明管线；**真机视觉验证需美术立绘 + 一次游戏启动**，
> 由后续 agent 在拿到图后执行。

---

## 8. 已知限制与下一步（v2 及以后）

1. **场景摆放（最高优先）**：v1 只注册数据。要让 NPC 站在地图，需
   - 方法 A：方法钩子（`method_hooks`）在目标场景 `_ready` 时实例化 `npc.gd` 节点于坐标；
   - 方法 B：场景 pck 覆盖层，在 `.tscn` 里加 NPC 节点。
   两者都依赖目标场景源码/结构可达——4.6 重写时部分场景逻辑缺失，需先补源码或取场景 pck。
2. **纹理 import**：若游戏用 `.import` 元数据而非裸 PNG 加载纹理，需在 pck 补
   `assets/npcs/npc90001/1_1.png.import`（Godot 4 纹理 import 约定）。先用裸 PNG 验证，
   失败再补。
3. **联机禁用**：本 mod 纯本地数据/资源，无联机代码，符合规范 R2；若 v2 加场景钩子，
   仍需避开 ENet/UDP/TCP/WebSocket 直连与 `api.host`（注释里也别写，见规范 L0-1）。
4. **多 mod 共存**：v2 改 `data_patches` 定点合并，避免与别的 `npcs.json` 覆盖 mod 冲突。
5. **4.7 出包**：用开发包自带的 `tools/jjm_pack.py seal --line 4.7` 补 `verification.json`
   （只有严格档会拦；但提前 seal 更省事，见第 5 节）。

---

## 9. 交接状态

- ✅ 数据驱动架构已确认（`game.gd` 加载 `npcs.json` → `npc.gd` 渲染）
- ✅ NPC 数据结构已从参考包提取（第 2 节）
- ✅ 脚手架就绪：`npc_definition.json` + `art_spec.md` + `build_npc_mod.py`
- ✅ 4.6 占位 mod 已构建并 `PckReader` 回读验证（9 条目，90001 已合入）
- ⏳ **美术立绘**：待工作室伙伴产出，按 `art_spec.md` 交付后替换占位帧
- ⏳ **4.7 出包**：基底 `npcs.json` 与 seal 工具都在手，随时可出（见第 5 节）
- ⏳ **场景摆放**：v2 待定（依赖目标场景源码可达性）
