# 素材权属声明（mecha 设计源图与版本对比）

本目录存放界面素材的**设计源图、加工中间产物与版本对比图**，不参与程序运行。
运行期实际加载的是 `app/ui/pyside/assets/mecha/`（见该目录下的 `NOTICE.md`）。

## 内容分类

| 类别 | 文件 | 说明 |
|---|---|---|
| 设计素材 | `logo.png`、`round.png`、`btn_a~d.png`、`title.png`、`background*.jpg`、`avatar.png`、`bilibili.png`、`btn_bar.png`、`panel.png` | 从官网宣传图抠取 / 从游戏包提取 |
| 加工中间产物 | `background_clean.jpg`、`background_logo_orig.jpg`、`background_1280_backup.jpg` | 去底、去标识、缩放的过程版本 |
| `v2/` 素材 | `v2/` 下的同名图片 | 第二轮视觉调整的源图 |
| 开发过程截图 | `_*.png`、`v2/_*.png`、`v2/pages/*.png` | 界面调试与版本对比截图，非素材 |

> 开发过程截图为本地留档，默认**不进入版本库**（见 `.gitignore`）。需要公开时再单独处理。

## 权属

《机甲旋风》的**名称、标识、角色形象与美术资源，著作权归原权利人所有**。
`bilibili.png` 是哔哩哔哩（Bilibili）的标识，权利归其所有者。

本项目对上述素材**不主张任何权利**，也未获得原权利人的授权或认可。
素材仅作为同人界面的展示与开发留档之用，不作商业用途。

## 许可边界

仓库根目录的 `LICENSE`（MIT）**只覆盖源代码**，不覆盖本目录下的任何图片文件。
详见 `THIRD_PARTY_NOTICES.md` 第 2.7 节。

## 来源

`tools/extract_mecha_ui.py`（官网宣传图抠取：去白底 + 抹除按钮文字），
以及游戏资源包内提取。脚本头部有完整的处理步骤说明。

## 移除方式

删除本目录不影响程序运行与构建——该目录不在 PyInstaller 的 `datas` 清单内。
