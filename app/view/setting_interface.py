"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-07 23:14:47
LastEditTime: 2025-02-11 19:20:13
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QWidget
from qfluentwidgets import ExpandLayout, PrimaryPushSettingCard, SwitchSettingCard
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import ScrollArea, SettingCardGroup

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from app.components.settings.custom_adb_setting_card import CustomAdbSettingCard
from app.components.settings.line_edit_setting_card import LineEditSettingCard
from app.components.settings.spin_box_setting_card import SpinBoxSettingCard
from core.model.emulator import emulator_list

MIRROR_URL = "https://mirrorchyan.com/zh/projects?rid=Auto_Resonance&source=auto-resonance-release"


class SettingInterface(ScrollArea):
    """Setting interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)

        # setting label
        self.settingLabel = QLabel("设置", self)

        # music folders
        self.musicInThisPCGroup = SettingCardGroup("配置", self.scrollWidget)
        self.lifecycleGroup = SettingCardGroup("任务资源管理", self.scrollWidget)
        self.selfHealingGroup = SettingCardGroup("Codex 自愈", self.scrollWidget)
        self.mirrorCdkCard = LineEditSettingCard(
            cfg.mirrorCdk,
            "Mirror酱 CDK",
            FIF.LABEL,
            "Mirror酱 CDK",
            parent=self.musicInThisPCGroup,
            isPassword=True,
        )
        self.mirrorCard = PrimaryPushSettingCard(
            "Mirror酱",
            FIF.SHARE,
            "浏览 Mirror 酱",
            "在 Mirror 酱官网购买 CDK",
            self.musicInThisPCGroup,
        )
        # self.goodsTypeCard = SwitchSettingCard(
        #     FIF.TAG,
        #     "数据源",
        #     "开为 KMou，关为 SRAP",
        #     configItem=cfg.goodsType,
        #     parent=self.musicInThisPCGroup,
        # )
        # self.uuidCard = LineEditSettingCard(
        #     cfg.uuid,
        #     "KMou商品请求 UUID",
        #     FIF.LABEL,
        #     "KMou商品请求 UUID",
        #     parent=self.musicInThisPCGroup,
        #     isPassword=True,
        # )
        # self.adbPathCard = LineEditSettingCard(
        #     cfg.adbPath,
        #     "ADB路径",
        #     FIF.PALETTE,
        #     "ADB程序路径",
        #     parent=self.musicInThisPCGroup,
        # )
        self.adbOrderCard = CustomAdbSettingCard(
            cfg.device,
            FIF.GAME,
            "ADB地址",
            "修改该内容自动变为自定义ADB端口",
            parent=self.musicInThisPCGroup,
        )
        self.autoGameLifecycleCard = SwitchSettingCard(
            FIF.PLAY,
            "任务自动管理游戏进程",
            "队列有任务时启动并管理当前 MuMu 多开实例和游戏",
            configItem=cfg.enableAutoGameLifecycle,
            parent=self.lifecycleGroup,
        )
        self.autoStartEmulatorCard = SwitchSettingCard(
            FIF.GAME,
            "模拟器未启动时自动开启",
            "按当前选择的 MuMu 安装路径和多开 index 精确启动对应实例",
            configItem=cfg.autoStartEmulator,
            parent=self.lifecycleGroup,
        )
        self.closeGameWhenIdleCard = SwitchSettingCard(
            FIF.POWER_BUTTON,
            "队列结束后关闭游戏",
            "关闭后会保留游戏；若同时关闭模拟器，游戏仍会随模拟器结束",
            configItem=cfg.closeGameWhenIdle,
            parent=self.lifecycleGroup,
        )
        self.closeEmulatorWhenIdleCard = SwitchSettingCard(
            FIF.POWER_BUTTON,
            "队列结束后同时关闭模拟器",
            "开启后，队列结束时还会关闭对应 MuMu 多开实例",
            configItem=cfg.closeEmulatorWhenIdle,
            parent=self.lifecycleGroup,
        )
        self.autoConfirmResourceUpdateCard = SwitchSettingCard(
            FIF.DOWNLOAD,
            "自动确认资源更新",
            "仅在识别到受控资源更新且大小未超过配置上限时确认",
            configItem=cfg.autoConfirmResourceUpdate,
            parent=self.lifecycleGroup,
        )
        self.personalStartupEpisodeCard = SwitchSettingCard(
            FIF.PLAY,
            "启动任务前自动准备游戏",
            "自动启动指定模拟器和游戏，处理已知启动页面，并恢复到后续任务可以接管的已知页面。不会固定前往某个城市。",
            configItem=cfg.enablePersonalStartupEpisode,
            parent=self.lifecycleGroup,
        )
        self.maximumResourceUpdateMbCard = SpinBoxSettingCard(
            cfg.maximumResourceUpdateMb,
            FIF.DOWNLOAD,
            "资源更新最大允许大小（MB）",
            "实际识别大小超过该上限时停止，不执行确认",
            spin_box_min=1,
            spin_box_max=102400,
            parent=self.lifecycleGroup,
        )
        self.codexSelfHealingCard = SwitchSettingCard(
            FIF.SYNC,
            "启用 Codex 自愈智能体",
            "异常会保留本地现场；开启后在隔离工作树中启动 Codex 诊断",
            configItem=cfg.enableCodexSelfHealing,
            parent=self.selfHealingGroup,
        )
        self.codexIsolatedRepairCard = SwitchSettingCard(
            FIF.SETTING,
            "允许生成隔离修复",
            "Codex 可在隔离沙箱内修改工作树并运行测试；候选仍需人工验证",
            configItem=cfg.allowCodexIsolatedRepair,
            parent=self.selfHealingGroup,
        )
        # self.adbOrderCard = LineEditSettingCard(
        #     cfg.adbOrder,
        #     "ADB地址",
        #     FIF.PALETTE,
        #     "ADB地址",
        #     parent=self.musicInThisPCGroup,
        # )
        self.mirrorCard.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(MIRROR_URL))
        )
        self.__initWidget()

    def __initWidget(self):
        self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("SettingInterface")

        # initialize style sheet
        self.scrollWidget.setObjectName("scrollWidget")
        self.settingLabel.setObjectName("settingLabel")
        StyleSheet.SETTING_INTERFACE.apply(self)

        # initialize layout
        self.__initLayout()

    def __initLayout(self):
        self.settingLabel.move(36, 30)

        # add cards to group
        # self.musicInThisPCGroup.addSettingCard(self.goodsTypeCard)
        # self.musicInThisPCGroup.addSettingCard(self.uuidCard)
        # self.musicInThisPCGroup.addSettingCard(self.adbPathCard)
        self.musicInThisPCGroup.addSettingCard(self.mirrorCdkCard)
        self.musicInThisPCGroup.addSettingCard(self.mirrorCard)
        self.musicInThisPCGroup.addSettingCard(self.adbOrderCard)
        self.lifecycleGroup.addSettingCard(self.autoGameLifecycleCard)
        self.lifecycleGroup.addSettingCard(self.autoStartEmulatorCard)
        self.lifecycleGroup.addSettingCard(self.closeGameWhenIdleCard)
        self.lifecycleGroup.addSettingCard(self.closeEmulatorWhenIdleCard)
        self.lifecycleGroup.addSettingCard(self.personalStartupEpisodeCard)
        self.lifecycleGroup.addSettingCard(self.autoConfirmResourceUpdateCard)
        self.lifecycleGroup.addSettingCard(self.maximumResourceUpdateMbCard)
        self.selfHealingGroup.addSettingCard(self.codexSelfHealingCard)
        self.selfHealingGroup.addSettingCard(self.codexIsolatedRepairCard)

        # add setting card group to layout
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.musicInThisPCGroup)
        self.expandLayout.addWidget(self.lifecycleGroup)
        self.expandLayout.addWidget(self.selfHealingGroup)

    def showEvent(self, event):
        """当切换到该页面时，触发这个事件"""
        super().showEvent(event)
        # 刷新adb设置
        self.adbOrderCard.update()
