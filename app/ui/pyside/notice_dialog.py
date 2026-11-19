"""首次启动的使用前须知（免费声明 + 免责条款）。

为什么单独做一个对话框而不是 ``QMessageBox``
------------------------------------------------
正文有七节、约 900 字，``QMessageBox`` 塞不下也排不开，而且它的
``setText`` 富文本在某些平台会被强制折成窄条。这里用 ``QDialog`` +
``QScrollArea``，排版完全可控。

三个设计约束（与产品要求一一对应）
----------------------------------
1. **同意才能用**：点「不同意」直接退出进程，不创建主窗口。
   接入点必须在 ``MainWindow(cfg)`` **之前** —— 否则背景会先闪一下，
   用户点了不同意却看见主界面被销毁，语义不成立。
2. **强制阅读 15 秒**：同意按钮初始禁用，按钮文案显示倒数
   （``15 秒后可以同意`` → ``我已知悉并同意``）。倒计时走 ``QTimer``，
   对话框关闭时停掉，避免定时器在已销毁的控件上回调。
3. **只征询一次**：结果写入 ``ui.notice_ack``（**版本字符串**而非 bool）。
   用字符串是为了将来条款改版时能升到 ``"2"`` 重新征询，
   不必让用户改配置。

与《免责声明.md》的关系（别再漏同步）
--------------------------------------
``扩展开发文档/公共文档/免责声明.md`` 是给扩展/改版作者看的开发包文档，
本弹窗是**玩家首启看到的协议**，两份独立排版、各自硬编码，互不读取。
因此**改任一份的功能条款时，必须同步另一份并把 ``NOTICE_VERSION`` +1**，
否则老玩家的 ``notice_ack`` 仍是旧版本，新条款等于没生效。
``tests/test_notice_dialog.py`` 会检查关键条款是否两边都在。

为什么这个键**不能**进出厂配置
------------------------------
出厂配置里若带着 ``notice_ack``，所有玩家第一次打开就直接跳过弹窗，
等于没做。它只应在用户点「同意」时写入。见
``tools/build_release_zip.py::release_launcher`` 里的剔除逻辑。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ... import edition

#: 当前条款版本。改文案时**必须**递增，否则老用户不会重新看到新条款。
#: v2（2026-11-11）：补进「系统内存优化」「自定义版本接入 / 改版创作者自备
#: 源码」「不支持联机接入」等条款；这些此前只写在开发包的免责声明里。
NOTICE_VERSION = "2"

#: 强制阅读时长（秒）。用户要求 15 秒。
HOLD_SECONDS = 15

#: 获取渠道（B 站主页，与 mecha_shell 里的创作者链接保持一致）
CHANNEL_URL = "https://space.bilibili.com/1278623966"
CHANNEL_TEXT = "space.bilibili.com/1278623966"

#: 正文。用 QLabel 富文本，``<b>`` 只加在四处关键结论上 ——
#: 全加粗等于没加粗。
_BODY = f"""
<style>
  body {{ color: #dbe9f8; }}
  h3 {{ color: #ffd68a; font-size: 14px; margin: 16px 0 6px 0; }}
  p  {{ line-height: 172%; margin: 4px 0 10px 0; }}
  li {{ line-height: 172%; margin: 2px 0; }}
  .warn {{ color: #ffb838; }}
</style>

<h3>一、本启动器免费提供</h3>
<p>
新起点启动器，以及通过本项目获取渠道发布的扩展（MOD），均不收取任何费用。<br/>
<b>如果你是通过付费购买的，那不是本项目发布的版本。请立即申请退款。</b><br/>
本项目从未授权任何个人或团体进行商业销售、代购、代安装或付费技术支持。
以「激活码」「会员」「内部版」「完整版」「去限制版」等名义收费的，均与本项目无关。
</p>

<h3>二、获取渠道与版本完整性</h3>
<p>
本项目仅通过以下渠道发布：<br/>
B 站：<a href="{CHANNEL_URL}" style="color:#4dc8ff;">{CHANNEL_TEXT}</a><br/>
从其他渠道获取的版本，无法保证其完整性与安全性，也不在本项目的维护范围内。
</p>
<p>
他人在本工具基础上二次打包、修改、汉化、精简、加壳、注入或重签名的版本，
与本项目无任何关系。<span class="warn">这类版本可能被植入恶意代码、捆绑软件或窃取信息的程序</span>，
本项目不对其行为及后果承担任何责任，也不提供任何形式的技术支持。
</p>

<h3>三、版权归属</h3>
<p>
<b>《机甲旋风》游戏本体及其全部美术、音乐、剧情、程序资源，
著作权归原权利人所有。</b>本工具不对上述任何内容主张权利。
</p>
<p>
本启动器<b>不包含游戏本体</b>，也不分发、破解、传播游戏本体或其中的任何资源。<br/>
本工具不提供游戏本体下载，不提供破解补丁，不修改、不绕过任何付费或授权机制，
不生成、不传播任何游戏资源的副本。你需要自行通过合法途径获取游戏本体。
</p>
<p>
各扩展（MOD）的著作权归其各自作者所有。启动器只提供加载与管理的技术手段，
不代表对任何 MOD 内容的认可、审核或主张。<br/>
MOD 的内容合法性、安全性及其是否侵犯第三方权利，由 MOD 作者与使用者自行负责。
</p>

<h3>四、第三方工具声明</h3>
<p>
本工具是独立开发的第三方兼容工具，<b>与游戏开发方、运营方及其关联方无任何关联</b>，
未获其授权、认可或背书。<br/>
本工具的使用不改变游戏本身的许可条款，你仍需遵守游戏原有的用户协议、
服务条款及所在地区的法律法规。
</p>
<p>
若游戏开发方或运营方变更技术方案、加密方式或服务条款，导致本工具部分或全部失效，
本项目不承诺修复时间，也不承担因此产生的任何损失。
</p>

<h3>五、本工具的能力边界（重要）</h3>
<p>
本工具是<b>纯本地</b>的单机扩展管理器，只做一件事：在游戏启动时按顺序叠加
你本机已有的扩展文件。它<b>不具备、也不提供</b>下列任何能力：
</p>
<ul>
  <li><b>不接入任何联机模块。</b>本项目明确禁止联机相关功能的接入与分发，
      不对线上服务、多人对战、房间匹配、排行榜等任何联网功能提供支持，
      也不会实现或协助实现此类功能。</li>
  <li><b>不提供热更新。</b>本工具不做在线自动更新，也不会在后台下载或替换
      任何文件。你需要更新时，请自行到获取渠道下载新版本。</li>
  <li><b>不修改线上账号数据。</b>不读写服务器端数据，不涉及账号体系、
      付费道具、虚拟货币或任何游戏内经济活动。</li>
  <li><b>不绕过任何校验。</b>不修改、不注入、不拦截游戏的完整性校验或
      安全机制，也不提供任何形式的破解、越权或加速功能。</li>
  <li><b>不获取、不上传任何游戏资源。</b>不生成游戏本体的副本，
      不把游戏资源发送到任何外部位置。</li>
</ul>
<p>
本工具的全部作用范围是<b>你本机上已存在的游戏目录与扩展文件</b>。
超出这一范围的任何用途，都不属于本项目的设计意图，也不在本项目的支持范围内。
</p>

<h3>六、自定义改版、扩展接入与系统内存优化</h3>
<p>
本工具支持把你自己的改版游戏登记为「自定义版本」。这类改版及其反解源码、
描述文件、能力与锚点索引<b>全部由改版创作者自行提供</b>；本工具只提供运行与
接入所需的框架，<b>不逆向、不生产、不补全</b>这些内容，也不为第三方改版本体的
内容、版权、安全性或运行行为背书。
</p>
<p>
登记时会按文件内容校验是否属于本系列的 Godot 改版本体。
<span class="warn">该校验只回答「文件是什么」，不构成无恶意代码、无联机行为、
不破坏存档的保证。</span>描述未通过一致性校验时，该版本只启动、不加载扩展；
通过校验也不代表扩展必然兼容，组合冲突仍需你自行验证。
若创作者要启用方法钩子类扩展，还需自行准备并通过验收的反解源码。
</p>
<p>
自定义版本的接入<b>同样不支持联机</b>：描述、扩展或创作者提供的源码中出现
P2P、直连等联机协议的，一律拒绝接入。因导入、运行第三方改版，或按其描述
挂载扩展造成的存档、数据与系统问题，由使用者自行承担。
</p>
<p>
设置中的「启动前释放系统内存」是<b>系统级操作、默认关闭</b>：它对本机其它程序
换出暂不使用的内存页，<b>不降低游戏本身的内存占用</b>，机械硬盘上可能出现
短时卡顿。是否开启由你自行决定并承担相应体验变化。
</p>

<h3>七、使用风险</h3>
<p>
本工具通过覆盖层叠加的方式改变游戏运行时的资源读取结果，
属于对本机文件的非常规使用，可能带来以下后果：
</p>
<ul>
  <li>存档异常、进度丢失、存档损坏或无法读取</li>
  <li>游戏崩溃、卡死、画面异常、音效缺失</li>
  <li>与其它本机 MOD、修改器、录屏或直播软件冲突</li>
  <li>因扩展之间互相冲突导致的加载失败或功能失效</li>
</ul>
<p>
<b>上述风险由使用者自行判断并承担。请在使用前自行备份存档与游戏本体。</b><br/>
本工具不接触、不影响任何线上账号或服务器数据，因此
<b>不承担与账号状态相关的任何责任</b>；同时也提醒你：
请勿将本工具用于任何违反游戏用户协议或当地法律法规的用途。
</p>

<h3>八、数据与隐私</h3>
<p>
本工具在本机运行，不上传你的存档、账号信息或游戏数据。<br/>
本工具会读取并写入以下本机内容，用于实现扩展挂载与存档隔离：
</p>
<ul>
  <li>游戏目录内的资源文件（只读取，除覆盖层外不做修改）</li>
  <li>本工具自身的配置、日志与缓存目录</li>
  <li>与存档隔离相关的存档目录</li>
</ul>
<p>
日志文件可能包含本机路径、文件名等诊断信息。
<span class="warn">在公开场合（论坛、群聊、问题反馈）贴出日志前，请自行检查并隐藏涉及个人隐私的路径与用户名。</span>
</p>

<h3>九、无担保声明</h3>
<p>
本工具按「现状」（AS IS）提供，不作任何明示或默示的担保，
包括但不限于适销性、特定用途适用性、无病毒、无错误及不侵权担保。<br/>
本项目不保证本工具能满足你的特定需求，也不保证运行不中断、无差错或与所有环境兼容。
</p>

<h3>十、责任限制</h3>
<p>
在法律允许的最大范围内，开发者、贡献者及分发者不对因使用或无法使用本工具
而产生的任何直接、间接、附带、特殊、惩罚性或后果性损失承担责任，
包括但不限于：
</p>
<ul>
  <li>数据丢失、存档损坏、设备损坏</li>
  <li>利润损失、业务中断、商誉损失</li>
  <li>因使用第三方 MOD 或二次打包版本导致的任何损失</li>
</ul>
<p>
即使已被告知上述损害的可能性，本条限制依然适用。
部分司法辖区不允许排除默示担保或限制附带损害责任，
若你所在地区法律另有强制规定，则以当地法律为准。
</p>

<h3>十一、使用资格与自行判断</h3>
<p>
你应具备完全民事行为能力。若你为未成年人，应在监护人知情并同意的前提下使用本工具，
并由监护人承担相应责任。<br/>
你是否使用本工具、如何使用、装载哪些 MOD，均由你自行判断并决定。
</p>

<h3>十二、条款的变更与终止</h3>
<p>
本须知可能在后续版本中更新，更新后随新版本一同发布。<br/>
继续使用新版本即视为接受更新后的内容；若不同意，请停止使用并删除本工具。<br/>
本项目保留在任何时候停止开发、停止分发或调整功能范围的权利，无需事先通知。
</p>

<h3>十三、其他</h3>
<p>
本须知的任何条款被认定无效或不可执行时，其余条款仍然有效。<br/>
本须知未尽事宜，依照本工具所在地区的法律法规处理。<br/>
本项目对本须知的解释与执行拥有最终说明权，但不排除你依法享有的权利。
</p>

<h3>十四、确认</h3>
<p>
<b>点击「我已知悉并同意」，表示你已完整阅读、理解并接受上述全部条款。</b><br/>
若你不同意其中任何一条，请点击「不同意并退出」关闭本程序，
并删除本工具的全部文件。
</p>
"""


class FirstRunNotice(QDialog):
    """首启须知。``exec()`` 返回 ``Accepted`` 才允许继续启动。

    用法::

        dlg = FirstRunNotice()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 1          # 用户不同意，调用方负责退出
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用前须知")
        # 固定尺寸：正文高度已定，缩放没有意义，还会让排版跑掉。
        self.setFixedSize(760, 640)
        self.setModal(True)
        # 整体深色底：不设的话对话框用系统主题色（浅灰），
        # 会和正文的深蓝块、橙金标题割裂成三种调子，很难看。
        self.setStyleSheet(
            "QDialog{background:#0a1830;}"
            "QLabel{background:transparent;}"
            # 按钮：深色底 + 青蓝描边，与启动器的机甲风一致。
            # 不设的话 Qt 用系统默认浅灰按钮，压在深色对话框上像贴上去的。
            "QPushButton{"
            "  background:#12294a; color:#dbe9f8;"
            "  border:1px solid #1d5892; border-radius:6px;"
            "  padding:7px 22px; font-size:13px; min-width:104px;"
            "}"
            "QPushButton:hover{background:#1a3c67; border-color:#00a8ff;}"
            "QPushButton:disabled{"
            "  background:#0e2038; color:#6f8aa8; border-color:#243a55;"
            "}"
        )

        self._remain = HOLD_SECONDS
        self._timer: QTimer | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        # --- 标题
        title = QLabel(f"{edition.app_name()} · 使用前须知")
        f = QFont("Microsoft YaHei", 15)
        f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color:#ffd68a;")
        root.addWidget(title)

        sub = QLabel(
            f"首次使用前请完整阅读以下条款。"
            f"倒计时结束前无法点击同意（{HOLD_SECONDS} 秒）。"
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#94b4d4; font-size:12px;")
        root.addWidget(sub)

        # --- 正文（可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea{background:rgba(10,24,48,0.72);"
            " border:1px solid #143e6c; border-radius:8px;}"
        )
        body = QLabel(_BODY)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)   # B 站链接可直接点开
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setContentsMargins(16, 14, 16, 14)
        body.setStyleSheet("background:transparent; font-size:13px;")
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # --- 底部按钮
        #
        # 顺序：**同意在左、不同意在右**。
        # 这是 Windows 对话框惯例（主操作靠左），与 macOS 相反。
        # 之前写反了，实测截图里「不同意并退出」在最右、还带着默认按钮
        # 的高亮边框，视觉上像是主操作 —— 会让用户误点。
        box = QDialogButtonBox(self)
        self.accept_btn = QPushButton(f"请阅读（{HOLD_SECONDS}）")
        self.reject_btn = QPushButton("不同意并退出")
        box.addButton(self.accept_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(self.reject_btn, QDialogButtonBox.ButtonRole.RejectRole)
        # 倒计时期间默认按钮落在「不同意」上：此时同意不可用，
        # 回车不该落在禁用按钮上（Qt 会把回车转给默认按钮，禁用则无响应，
        # 用户会以为卡了）。解锁后再把默认切回同意。
        self.accept_btn.setDefault(False)
        self.accept_btn.setEnabled(False)
        # 禁用态样式统一走上面 QDialog 的 :disabled 规则，
        # 不再单独设内联样式表 —— 内联优先级更高，会把 hover 规则压掉。
        self.reject_btn.setDefault(True)
        # 用户要求「不同意禁止使用」：Reject 必须真的退出，
        # 这里不接 reject()，由调用方按 DialogCode 处理。
        self.reject_btn.clicked.connect(self.reject)
        self.accept_btn.clicked.connect(self.accept)
        root.addWidget(box)

        # 倒计时在 showEvent 里启动 —— 构造时启动会在用户还没看到窗口
        # 时就开始跑（模态框弹出前可能有几百毫秒的主窗初始化）。
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ 倒计时

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self._timer is not None and not self._timer.isActive():
            self._tick()          # 先把文案刷成 15
            self._timer.start()

    def _tick(self) -> None:
        self._remain -= 1
        if self._remain > 0:
            self.accept_btn.setText(f"请阅读（{self._remain}）")
            return
        # 倒计时结束：解锁并换回正式文案
        if self._timer is not None:
            self._timer.stop()
        self.accept_btn.setText("我已知悉并同意")
        self.accept_btn.setEnabled(True)
        self.accept_btn.setDefault(True)
        self.reject_btn.setDefault(False)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 关闭（含点 X、Alt+F4）一律视为不同意 —— 不能靠关窗绕开声明。
        if self._timer is not None:
            self._timer.stop()
        super().closeEvent(event)

    def reject(self) -> None:  # noqa: N802 - Qt 命名
        if self._timer is not None:
            self._timer.stop()
        super().reject()