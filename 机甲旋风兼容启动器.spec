# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：只打包图形启动器（PySide6）。

    pyinstaller 机甲旋风兼容启动器.spec --noconfirm --distpath dist --workpath builds/_pyi

产物是 onedir 形式。``config/``、``baseline/``、``engines/``、``schemas/``、
``runtime/`` 这些**数据目录不在这里打包**——冻结后 ``paths.project_root()``
返回 exe 所在目录，发布时需要把它们复制到 exe 同级（见 docs/规范手册.md 8.4）。
"""

import os
import time

from PyInstaller.utils.hooks import collect_submodules

#: spec 所在目录（PyInstaller 注入 SPECPATH）。原先写死 ``D:/jjxf-launcher/...``，
#: 换个目录就没法打包；改成相对 spec 解析。
_HERE = globals().get("SPECPATH") or os.getcwd()

# ---------------------------------------------------------------- 产物名
#
# **改程序名只改这一行**（2026-11-08 统一命名为「新起点启动器」）。
#
# 这个值同时决定：
#   · 生成的 exe 文件名（``新起点启动器.exe``）
#   · COLLECT 出来的目录名（``dist/新起点启动器/``）
#
# 改完之后要同步的三处（都已改为从这里派生，只需核对）：
#   1. ``app/edition.py`` 的 ``EXE_STEM``  —— 卸载逻辑与 CLI 提示读它
#   2. ``tools/build_release_zip.py`` 的 ``INCLUDE_FROM_DIST`` 白名单
#   3. spec 文件名本身（不影响产物，但按项目惯例应当一致）
_EXE_NAME = 'New state launcher'


# ---------------------------------------------------------------- 版本烧入
#
# 玩家版 / 开发者版**同源**，差异是一个构建期常量。这里按 JJXF_EDITION
# 生成 ``app/_build_edition.py``，由 ``app/edition.py`` 在冻结态导入。
#
# 为什么不再「手工改 edition.py」：这次就是手工切换漏了一步，两个包都烧成
# 了 dev，玩家版行为被「升」成开发者版。构建期写文件 + 校验，才是可重复的。
_EDITION = (os.environ.get('JJXF_EDITION') or 'unified').strip().lower()
#
# 2026-11-08 新增 ``unified``：**对外唯一发布的版本**。
#
# 原来分 player / dev 两个包，玩家要选「我该下哪个」，作者要维护两份
# 构建配置。现在合成一个 —— 同一份 exe，差别只在「设置 → 开发者模式」
# 开不开（见 app/ui/pyside/features.py）。
#
# 保留 player / dev 是因为：
#   · player：`config.netplay_open()` 里有 ``is_player()`` 硬闸门，
#     不能删掉那个值（否则历史配置判断会变）；
#   · dev：内部调试时想跳过某些玩家侧限制，仍需要一个标记。
if _EDITION not in ('unified', 'player', 'dev', 'publisher'):
    raise SystemExit(
        f'JJXF_EDITION 只能是 unified / player / dev / publisher，收到：{_EDITION!r}'
    )

#: 构建标识：由装配脚本传 JJXF_BUILD_ID（形如 20261104-1830）；
#: 没传就按当前时间生成。它会被烧进 exe，并写进包根 BUILD_INFO.json，
#: 启动时两边对不上就说明「解压覆盖成了混合安装」。
_BUILD_ID = (os.environ.get('JJXF_BUILD_ID') or time.strftime('%Y%m%d-%H%M%S')).strip()

_BUILD_EDITION = os.path.join(_HERE, 'app', '_build_edition.py')
if os.path.exists(_BUILD_EDITION):
    os.remove(_BUILD_EDITION)  # 清掉上一次残留，避免拿错版本
with open(_BUILD_EDITION, 'w', encoding='utf-8', newline='\n') as _f:
    _f.write(
        '"""构建期生成的版本常量（由 spec 写入，勿手改）。"""\n\n'
        f'EDITION = "{_EDITION}"\n'
        f'BUILD_ID = "{_BUILD_ID}"\n'
    )


def _cleanup_build_edition() -> None:
    """构建结束把生成文件删掉，源码态永远按 dev 跑。"""
    try:
        os.remove(_BUILD_EDITION)
    except OSError:
        pass


import atexit

atexit.register(_cleanup_build_edition)


datas = [
    (os.path.join(_HERE, 'app', 'launcher.ico'), 'app'),
    # 同一份常量再以数据文件放进 _internal/app/：发布脚本据此核对
    # 「这个 dist 到底烧的是 player 还是 dev」，不再依赖打包脚本自己写的标记。
    (_BUILD_EDITION, 'app'),
    # 机甲风外壳的位图素材（LOGO / 圆形按钮 / 导航条 / 背景 / 头像 / B站字标）。
    # 必须整目录带上：mecha_assets.asset_dir() 在 frozen 下从
    # sys._MEIPASS/app/ui/pyside/assets/mecha/ 找，缺了就整壳降级。
    (os.path.join(_HERE, 'app', 'ui', 'pyside', 'assets', 'mecha'),
     'app/ui/pyside/assets/mecha'),
]

# Qt 的标准按钮文案（Yes/No/OK/Cancel）来自翻译文件，默认是英文。
# 不带上这两个 .qm，中文界面里会弹出「Yes / No」，且 QMessageBox 的
# 内部文字样式也会走英文度量。带上后 app.py 里的 QTranslator 才能加载。
try:
    import PySide6

    _translations = os.path.join(os.path.dirname(PySide6.__file__), 'translations')
    for _name in ('qtbase_zh_CN.qm', 'qt_zh_CN.qm'):
        _path = os.path.join(_translations, _name)
        if os.path.exists(_path):
            datas.append((_path, 'PySide6/translations'))
except Exception:  # noqa: BLE001 - 拿不到翻译文件不该拦住建包
    pass

binaries = []
hiddenimports = [
    'app._build_edition',
    'app.core.build.package',
    'app.core.build.godot',
]
hiddenimports += collect_submodules('app.core')
hiddenimports += collect_submodules('app.core.ext')
hiddenimports += collect_submodules('app.core.pck')
hiddenimports += collect_submodules('app.core.build')
hiddenimports += collect_submodules('app.ui.pyside')


a = Analysis(
    [os.path.join(_HERE, 'build_launcher.py')],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'pydoc_data',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'pytest',
        'PyQt5',
        'PyQt6',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(_HERE, 'app', 'launcher.ico')],
    version=os.path.join(_HERE, 'version_info.txt'),
    # 显式固化应用清单（含 longPathAware）：PyInstaller 默认值虽已带该声明，
    # 但会随版本/构建环境变化，回退后深目录安装扩展会静默失败。
    manifest=os.path.join(_HERE, 'app', 'launcher.manifest'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=_EXE_NAME,
)
