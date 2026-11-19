# 第三方组件声明（THIRD PARTY NOTICES）

> **状态：草案，待人工复核后随发布包附上。**
> 整理日期：2026-10-29
> 适用：`机甲旋风兼容启动器`（PySide6 版）及其发布目录。
>
> 本声明基于本机 `venv/Lib/site-packages/*.dist-info/METADATA` 与发布目录
> `dist/机甲旋风兼容启动器/_internal/` 的**实测扫描**整理。许可选项与全文以各组件
> 官方发布为准；带 `⚠️` 的条目表示**尚缺许可文本/需人工确认**，发布前必须补齐。
> 本文不构成法律意见。

---

## 1. 本作品自身的权属声明

本启动器桌面界面及业务层代码由「柳江巷」在 AI 辅助下完成，人类负责需求定义、
架构设计、技术决策、逐轮取舍与验证。**未沿用「柳江巷版 V1.45.1」(tkinter) 的旧代码**，
两者的定量对比见 `docs/evidence/原创性对比结果.txt`。

本启动器**不包含《机甲旋风》游戏本体**，仅提供扩展(mod)挂载与管理能力；游戏本体的
著作权归原权利人所有。

---

## 2. 随产品分发的组件

### 2.1 Qt for Python (PySide6) / Shiboken6 — 6.12.0 ⚠️

- **许可**：`LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`（三选一；另有 Qt 商业许可）。
- **发布包内位置**：`_internal/PySide6/`、`_internal/shiboken6/`。
- **实测**：发布包内**没有任何 LICENSE / COPYING 文本** → 不符合上述许可对
  「保留版权与许可声明」的要求。
- **发布前需补齐**（按你选择的许可分支）：
  - [ ] 若按 **LGPL-3.0** 分发：附 `LGPL-3.0` 全文 + 对应 `GPL-3.0` 全文、版权声明，
        并提供所用 LGPL 库的源码获取途径 / 允许反向工程、允许替换该库；
        对 PySide6/Shiboken6 库本身的修改需以相同许可开源。
  - [ ] 全文来源：Qt 官方安装包，或 <https://www.gnu.org/licenses/>（LGPL-3.0、GPL-3.0）。
  - [ ] 版权声明原文：参考包内 `LicenseRef-Qt-Commercial.txt`（Qt 商业许可引用，
        已存 `docs/evidence/licenses/`）及 Qt 发行包中的版权头。
- **注意**：PySide6 是 LGPL，通常**不要求**你的应用开源；但「附许可全文 + 允许替换/反工程」
  这些义务必须履行，不能只放一个空声明文件。

### 2.2 Python 运行时 — 3.13 ⚠️

- **许可**：Python Software Foundation License (PSF-2.0)。
- **发布包内位置**：`_internal/python3.dll`、`_internal/python313.dll`、`base_library.zip`。
- **义务**：保留 PSF 版权与许可声明。
- [ ] 发布前需附 PSF License 全文与版权声明（来源：<https://docs.python.org/3/license.html>）。

### 2.3 OpenSSL — 3.x ⚠️

- **许可**：Apache License 2.0。
- **发布包内位置**：`_internal/libcrypto-3-x64.dll`、`_internal/libssl-3-x64.dll`。
- **义务**：保留版权、许可与 NOTICE 声明（Apache-2.0 第 4 条）。
- [ ] 发布前需附 Apache-2.0 全文与 OpenSSL 版权/NOTICE。

### 2.4 Microsoft Visual C++ 运行库 — VCRUNTIME140*.dll

- **许可**：微软 Visual Studio 再分发条款（Microsoft Visual C++ Redistributable）。
- 随应用再分发通常**允许**，保持随附即可，无需额外开源义务。

### 2.5 Godot Engine — 4.6 ⚠️

- **许可**：MIT License。
- **位置**：`runtime/host46/jjxf46.exe`（约 164MB，编辑器构建）。
- **义务**：MIT 要求**保留版权与许可声明**。
- **实测**：`runtime/` 目录下**没有 LICENSE/COPYING 文本** → 不合规。
- [ ] 发布前需从 Godot 官方仓库取 `LICENSE.txt` / `COPYRIGHT.txt` 放进包内
      （来源：<https://github.com/godotengine/godot> 对应 tag）。

### 2.6 Pillow — 11.3.0（仅源码分发时适用）

- **许可**：`MIT-CMU`。
- **实测**：Pillow **未打进发布包**（`_internal` 内无 PIL 目录）；仅构建期工具
  `tools/make_icon.py` 引用。因此**发布包无需附其许可**。
- 若你对外分发**源码**，则需附 Pillow 许可全文——真实全文已存
  `docs/evidence/licenses/Pillow-11.3.0-LICENSE`（76KB，从包内复制）。

### 2.7 游戏美术素材（官网宣传图 / 游戏包提取）⚠️

- **位置与体积**（2026-11-19 实测）：
  - `app/ui/pyside/assets/mecha/`：7 个文件 / 约 4.0 MB，**运行期实际加载**；
  - `assets/ui/mecha/`：设计源图与加工中间产物 / 约 13.5 MB，**不参与运行、不入发布包**。
- **内容**：原作标识（`logo.png`）、角色头像（`avatar.png`）、背景立绘
  （`background*.jpg`）、按钮与面板贴图（`round.png` / `btn_bar.png` / `panel.png` /
  `btn_a~d.png`）、B 站图标（`bilibili.png`）。
- **来源**：一部分由 `tools/extract_mecha_ui.py` 从《机甲旋风》官网宣传图抠取
  （去白底 + 抹除按钮文字）；另一部分从游戏资源包内提取（`assets/ui/mecha/v2/`）。
- **权属**：《机甲旋风》的名称、标识、角色形象与美术资源，**著作权归原权利人所有**；
  `bilibili.png` 为哔哩哔哩（Bilibili）的标识，权利归其所有者。
  本项目对上述素材**不主张任何权利**，也未获得原权利人的授权或认可。
  素材仅作同人界面的展示之用随本工具分发，**不作商业用途**。
- **许可边界**：仓库根目录的 `LICENSE`（MIT）**仅覆盖源代码**，
  **不覆盖** `app/ui/pyside/assets/mecha/` 与 `assets/ui/mecha/` 下的任何图片文件。
- **处理约定**：权利人提出异议时，删除上述两个目录即完成移除。素材为纯装饰，
  缺失时 `mecha_assets.pixmap()` 返回空图，各绘制点走纯色 / 文字降级路径，
  **程序功能不受影响**（`assets/ui/mecha/` 本就不在打包清单内）。
- **保留决策**：2026-11-19 由项目作者决定「保留并标注」，不替换为自绘素材；
  相应免责声明已同时写入两个目录下的 `NOTICE.md`。

---

## 3. 仅构建 / 测试期，不随产品分发

以下组件只在本机 `venv` 中用于打包、测试，发布包内不含对应文件，
**发布包无需附其许可**；若分发完整开发环境则需另行处理。

| 组件 | 版本 | 许可 |
| --- | --- | --- |
| PyInstaller | 6.22.3 | GPLv2-or-later + bootloader 例外（允许打包分发非自由/商业程序，不污染你的代码） |
| pylnk3 | 0.4.3 | LGPL |
| pytest | 9.2.0 | MIT |
| altgraph | 0.17.5 | MIT |
| pefile | 2024.8.26 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| typing_extensions | 4.16.0 | PSF-2.0 |
| pycparser | 3.12 | BSD-3-Clause |
| pygments / pluggy / iniconfig / colorama | — | BSD-2 / MIT / MIT / BSD |

---

## 4. 网页版残留依赖（已实测未打进发布包）

网页版曾被删除，venv 里仍留有这些包，但**源码已不再引用、发布包实测也不含**对应文件
（`_internal` 内无 webview/bottle/clr/WebEngine/cffi）：

| 组件 | 版本 | 许可 |
| --- | --- | --- |
| pywebview | 6.2.1 | BSD 3-Clause |
| bottle | 0.13.4 | MIT |
| pythonnet | 3.2.1 | MIT |
| clr_loader / cffi / proxy_tools | — | MIT / MIT-0 / MIT |

这些不影响发布包合规；建议清理 venv 以减小体积、避免误判。

---

## 5. 发布前必须完成的动作（对应主清单 P0/P1）

1. ⚠️ 补齐 Qt/PySide6 的 LGPL（或所选分支）许可全文与版权声明 → `_internal/` 或包根。
2. ⚠️ 补 `engines/` 下 Godot 的 MIT 许可文本。
3. ⚠️ 补 PSF、OpenSSL(Apache-2.0) 的版权与许可声明。
4. ⚠️ 清理发布目录中的 `packs/`（19 个第三方扩展，367MB）、`profiles/`（测试存档）、
   `builds/`、`cache/`、`logs/`，并重置 `config/` 为不含本机路径的默认值——
   第三方扩展与存档的分发需单独授权，详见 `docs/权属归档与第三方许可清单.md` 3.2 节。

---

*本草案由实测扫描生成；许可全文与版权声明请从各组件官方渠道取得并核对后再发布。*
