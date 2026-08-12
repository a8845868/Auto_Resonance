"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-02 19:27:03
LastEditTime: 2025-02-11 19:11:04
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""
from typing import Union

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget
from loguru import logger
from qfluentwidgets import DotInfoBadge
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    InfoBadgePosition,
    InfoBar,
    InfoBarPosition,
    FluentWindow,
    NavigationItemPosition,
    SplashScreen,
    FluentIconBase,
    SystemThemeListener,
    TransparentToolButton,
    isDarkTheme,
    setTheme,
    MessageBox,
)

import app.common.resource  # 图标数据
from app.common.config import VERSION, cfg, isWin11
from app.common.signal_bus import signalBus
from app.components.update_message_box import UpdateMessageBox
from app.utils.constants import ICON_PATH, ROOT_PATH
from app.utils.utils import is_chinese
from app.view.two_city_run_business_interface import TwoRunBusinessInterface
from app.view.book_planner_interface import BookPlannerInterface
from app.view.inventory_interface import InventoryInterface
from app.view.gacha_planner_interface import GachaPlannerInterface
from app.view.passenger_planner_interface import PassengerPlannerInterface
from app.view.shop_planner_interface import ShopPlannerInterface
from core.utils.update.base_update_utils import UpdateStatus
from core.utils.update.mirror_update_utils import MirrorUpdateUtils
from core.services.personal_automation_entry import build_personal_startup_queued_task

from .adb_data_interface import ADBDataInterface
from .codex_debug_interface import CodexDebugInterface
from .dashboard_interface import DashboardInterface
from .task_settings_interface import (
    FatiguePlannerInterface,
    ResidentActivityInterface,
    RewardCollectionInterface,
)
from .setting_interface import SettingInterface


class MainWindow(FluentWindow):

    def __init__(self):
        super().__init__()
        self.wights = {}
        self._closeRetryScheduled = False

        # 主题监听器
        self.themeListener = SystemThemeListener(self)

        self.initWindow()
        self.initSystemTray()
        self.setInterface()

        self.initNavigation()
        self.navigationInterface.setExpandWidth(190)
        self.navigationInterface.expand(useAni=False)

        self.connectSignalToSlot()

        self.splashScreen.finish()
        # 检查更新
        self.updater = MirrorUpdateUtils()
        # self.checkUpdate()
        # 启用主题监听器
        self.themeListener.start()
        
        self.checkChinesePath()

    def connectSignalToSlot(self):
        signalBus.switchToCard.connect(self.switchToCard)
        # 监听主题切换
        cfg.themeChanged.connect(setTheme)

    def initNavigation(self):
        self.addSubInterface(self.homeInterface, FIF.HOME, "主页")
        self.addSubInterface(self.debugInterface, FIF.DEVELOPER_TOOLS, "调试")
        self.addSubInterface(self.residentActivityInterface, FIF.PLAY, "扫荡配置")
        self.addSubInterface(self.rewardCollectionInterface, FIF.ACCEPT, "领取任务奖励")
        self.addSubInterface(self.fatiguePlannerInterface, FIF.CAFE, "疲劳规划")
        self.addSubInterface(self.bookPlannerInterface, FIF.CALENDAR, "进货书规划")
        self.addSubInterface(self.inventoryInterface, FIF.ALBUM, "货币规划")
        self.addSubInterface(self.gachaPlannerInterface, FIF.SHOPPING_CART, "抽卡规划")
        self.addSubInterface(self.passengerPlannerInterface, FIF.PEOPLE, "客运规划")
        self.addSubInterface(self.shopPlannerInterface, FIF.SHOPPING_CART, "商店自动购买")
        self.addSubInterface(self.two_run_business_interface, FIF.TRAIN, "端点跑商")
        self.addSubInterface(self.adb_data_interface, FIF.GAME, "ADB信息")

        # 底部按钮
        self.updateButton = self.navigationInterface.addItem(
            routeKey="Update",
            icon=FIF.UPDATE,
            text="更新",
            onClick=self._update,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.settingInterface,
            FIF.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

    def initWindow(self):
        self.resize(1280, 820)
        self.setMinimumSize(1050, 700)
        self.setWindowIcon(QIcon(str(ICON_PATH / "logo.ico")))
        self.setWindowTitle(f"黑月无人驾驶 - {VERSION}")

        self.setMicaEffectEnabled(isWin11())
        self.setResizeEnabled(True)

        # create splash screen
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        desktop = self.screen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)
        self.show()
        QApplication.processEvents()

    def initSystemTray(self):
        """Add an explicit title-bar action that keeps automation in the tray."""
        self.trayIcon = QSystemTrayIcon(self.windowIcon(), self)
        self.trayIcon.setToolTip(self.windowTitle())
        tray_menu = QMenu(self)
        restore_action = QAction("显示主窗口", self)
        quit_action = QAction("退出", self)
        restore_action.triggered.connect(self.restoreFromTray)
        quit_action.triggered.connect(self.close)
        tray_menu.addAction(restore_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.trayIcon.setContextMenu(tray_menu)
        self.trayIcon.activated.connect(self._onTrayActivated)

        self.trayButton = TransparentToolButton(FIF.DOWN, self.titleBar)
        self.trayButton.setFixedSize(46, 32)
        self.trayButton.setToolTip("最小化到托盘")
        self.trayButton.clicked.connect(self.minimizeToTray)
        self.titleBar.buttonLayout.insertWidget(0, self.trayButton)

    def minimizeToTray(self):
        if not self._systemTrayAvailable():
            self.showMinimized()
            return
        self.trayIcon.show()
        self.hide()

    def _systemTrayAvailable(self):
        return QSystemTrayIcon.isSystemTrayAvailable()

    def restoreFromTray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _onTrayActivated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restoreFromTray()

    def setInterface(self):
        # create sub interface
        self.homeInterface = DashboardInterface(self)
        self.debugInterface = CodexDebugInterface(self)
        self.residentActivityInterface = ResidentActivityInterface(self)
        self.rewardCollectionInterface = RewardCollectionInterface(self)
        self.fatiguePlannerInterface = FatiguePlannerInterface(self)
        self.settingInterface = SettingInterface(self)
        self.bookPlannerInterface = BookPlannerInterface(self)
        self.inventoryInterface = InventoryInterface(self)
        self.gachaPlannerInterface = GachaPlannerInterface(self)
        self.passengerPlannerInterface = PassengerPlannerInterface(self)
        self.shopPlannerInterface = ShopPlannerInterface(self)
        self.two_run_business_interface = TwoRunBusinessInterface(self)
        self.homeInterface.setBusinessTaskProvider(
            self.two_run_business_interface.buildQueuedTask
        )
        self.homeInterface.addPriorityTaskProvider(
            lambda: build_personal_startup_queued_task()
            if bool(cfg.enablePersonalStartupEpisode.value)
            else None
        )
        self.homeInterface.addPriorityTaskProvider(
            self.fatiguePlannerInterface.buildQueuedTask
        )
        self.homeInterface.addTaskProvider(self.passengerPlannerInterface.buildQueuedTask)
        self.homeInterface.addTaskProvider(self.shopPlannerInterface.buildQueuedTask)
        for page in (
            self.residentActivityInterface,
            self.rewardCollectionInterface,
            self.fatiguePlannerInterface,
            self.two_run_business_interface,
            self.passengerPlannerInterface,
            self.shopPlannerInterface,
        ):
            page.scheduleCard.scheduleChanged.connect(
                self.homeInterface.refreshScheduleOverview
            )
        self.homeInterface.refreshScheduleOverview()
        self.homeInterface.activityStateChanged.connect(
            self.residentActivityInterface.setRunState
        )
        self.adb_data_interface = ADBDataInterface(self)

        self.update_message_box = UpdateMessageBox(self)

    def addSubInterface(
        self,
        interface: QWidget,
        icon: Union[FluentIconBase, QIcon, str],
        text: str,
        selectedIcon=None,
        position=NavigationItemPosition.TOP,
        isTransparent=False,
    ):
        super().addSubInterface(
            interface,
            icon,
            text,
            position=position,
            isTransparent=isTransparent,
        )
        self.wights[interface.objectName()] = interface

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # Newer PySide6/qframelesswindow versions may deliver an early resize
        # event from MSFluentWindow.__init__ before initWindow creates the
        # splash screen.
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def switchToCard(self, routeKey):
        """切换到指定界面"""
        self.switchTo(self.wights[routeKey])

    def closeEvent(self, e):
        queue_stopped = self.homeInterface.shutdown()
        scan_stopped = self.adb_data_interface.shutdown()
        self.debugInterface.shutdown()
        if not queue_stopped or not scan_stopped:
            e.ignore()
            if not self._closeRetryScheduled:
                self._closeRetryScheduled = True
                QTimer.singleShot(250, self._retryClose)
            return
        self.trayIcon.hide()
        self._stopThemeListener()
        super().closeEvent(e)

    def _stopThemeListener(self):
        """Synchronize native listener shutdown before its QObject is deleted."""
        try:
            self.themeListener.requestInterruption()
            self.themeListener.terminate()
            self.themeListener.wait(1000)
            self.themeListener.deleteLater()
        except RuntimeError:
            # Qt may deliver a second close event after the listener has
            # already been deleted.
            pass
    def _retryClose(self):
        self._closeRetryScheduled = False
        self.close()

    def _onThemeChangedFinished(self):
        super()._onThemeChangedFinished()

        # 云母特效启用时需要增加重试机制
        if self.isMicaEffectEnabled():
            QTimer.singleShot(100, lambda: self.windowEffect.setMicaEffect(self.winId(), isDarkTheme()))

    def _update(self):
        """
        检查更新
        """
        update_status = self.updater.get_update_status(cfg.mirrorCdk.value, reload=True)
        if update_status == UpdateStatus.UPDATE:
            self.update_message_box.show(cfg.mirrorCdk.value)
        elif update_status == UpdateStatus.FAILED:
            InfoBar.error(
                title="检查更新失败",
                content="请稍后重试",
                orient=Qt.Orientation.Horizontal,
                isClosable=False,
                position=InfoBarPosition.TOP,
                duration=1000,
                parent=self,
            )
        elif update_status == UpdateStatus.NOSUPPORT:
            InfoBar.error(
                title="更新程序只支持打包成exe后运行",
                content="",
                orient=Qt.Orientation.Horizontal,
                isClosable=False,
                position=InfoBarPosition.TOP,
                duration=1000,
                parent=self,
            )
        elif update_status == UpdateStatus.LATEST:
            InfoBar.success(
                title="当前已是最新版本",
                content="",
                orient=Qt.Orientation.Horizontal,
                isClosable=False,
                position=InfoBarPosition.TOP,
                duration=1000,
                parent=self,
            )
        elif update_status == UpdateStatus.FAILDCDK:
            InfoBar.error(
                title="Mirror CDK校验失败",
                content="请检查Mirror CDK是否正确",
                orient=Qt.Orientation.Horizontal,
                isClosable=False,
                position=InfoBarPosition.TOP,
                duration=1000,
                parent=self,
            )
        else:
            InfoBar.error(
                title="检查更新失败",
                content="请稍后重试",
                orient=Qt.Orientation.Horizontal,
                isClosable=False,
                position=InfoBarPosition.TOP,
                duration=1000,
                parent=self,
            )

    def checkUpdate(self):
        update_status = self.updater.get_update_status(cfg.mirrorCdk.value)
        if update_status == UpdateStatus.UPDATE and self.updateButton is not None:
            self.updateBadge = DotInfoBadge.error(
                parent=self.navigationInterface,
                target=self.updateButton,
                position=InfoBadgePosition.NAVIGATION_ITEM,
            )
            self.updateBadge.setFixedSize(10, 10)

    def checkChinesePath(self):
        if is_chinese(str(ROOT_PATH)):
            w = MessageBox("兼容性警告", "程序运行在中文路径上，请移动至英文路径", self)
            
            w.yesButton.setText("知道了")
            w.cancelButton.hide()
            w.buttonLayout.insertStretch(1)
            w.exec()
