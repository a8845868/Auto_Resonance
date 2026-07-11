"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-02 19:12:22
LastEditTime: 2025-02-11 19:08:33
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    ScrollArea,
    SwitchButton,
    isDarkTheme
)

from app.common.config import REPO_URL, cfg
from app.common.style_sheet import StyleSheet
from app.components.button_card import ButtonCardView
from app.components.link_card import LinkCardView
from app.components.settings.checkbox_group_card import CheckboxGroup
from app.utils.constants import ICON_PATH
from core.control.control import stop
from auto.resident_activity import (
    FULL_REALM_REWARDS,
    SIEGE_REWARDS,
    SIEGE_TASKS,
    run_resident_activity,
    run_resident_activity_once,
)
from auto.reward_collection import collect_rewards
from app.utils.worker import Worker
from qfluentwidgets import qconfig


def run_resident_activity_with_rewards(
    task: str,
    full_realm_reward: str,
    daily_activity: bool,
    travel_manual: bool,
):
    """Run the selected activity, then collect the enabled reward groups."""
    result = run_resident_activity(task, full_realm_reward)
    if daily_activity or travel_manual:
        collect_rewards(daily_activity, travel_manual)
    return result


REWARD_ICON_DIR = Path(__file__).resolve().parents[2] / "resources" / "rewards"


class RewardChoiceCard(QWidget):
    """Compact, reward-first selector card."""

    selected = Signal(str)

    def __init__(self, value, title, subtitle, icon_name, parent=None):
        super().__init__(parent)
        self.value = value
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(82)

        icon = QLabel(self)
        pixmap = QPixmap(str(REWARD_ICON_DIR / icon_name))
        icon.setPixmap(
            pixmap.scaled(
                56,
                56,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        icon.setFixedSize(60, 60)
        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        subtitle_label = QLabel(subtitle, self)
        subtitle_label.setStyleSheet("font-size: 11px; color: #7a7a7a;")

        text_layout = QVBoxLayout()
        text_layout.setSpacing(3)
        text_layout.addWidget(title_label)
        text_layout.addWidget(subtitle_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(icon)
        layout.addLayout(text_layout, 1)
        self.setChecked(False)

    def setChecked(self, checked):
        border = "#4f8cff" if checked else "rgba(128,128,128,0.30)"
        background = "rgba(79,140,255,0.16)" if checked else "rgba(128,128,128,0.06)"
        self.setStyleSheet(
            f"RewardChoiceCard {{ border: 2px solid {border}; "
            f"border-radius: 10px; background: {background}; }}"
        )

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.selected.emit(self.value)


class BannerWidget(QWidget):
    """Banner widget"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setFixedHeight(336)

        self.vBoxLayout = QVBoxLayout(self)
        self.titleLabel = QLabel("黑月无人驾驶", self)
        self.banner = QPixmap(ICON_PATH / "header.png")
        self.linkCardView = LinkCardView(self)

        self.__initWidget()
        self.loadSamples()

    def __initWidget(self):
        self.titleLabel.setObjectName("galleryLabel")

        self.vBoxLayout.setSpacing(0)
        self.vBoxLayout.setContentsMargins(0, 20, 0, 0)
        self.vBoxLayout.addWidget(self.titleLabel)
        self.vBoxLayout.addWidget(self.linkCardView, 1, Qt.AlignmentFlag.AlignBottom)
        self.vBoxLayout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def loadSamples(self):
        self.linkCardView.addCard(
            FluentIcon.GITHUB, "GitHub repo", "黑月无人驾驶", REPO_URL
        )

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        w, h = self.width(), self.height()
        path.addRoundedRect(QRectF(0, 0, w, h), 10, 10)
        path.addRect(QRectF(0, h - 50, 50, 50))
        path.addRect(QRectF(w - 50, 0, 50, 50))
        path.addRect(QRectF(w - 50, h - 50, 50, 50))
        path = path.simplified()

        # 初始化线性渐变效果
        gradient = QLinearGradient(0, 0, 0, h)

        # 绘制背景颜色
        if not isDarkTheme():
            gradient.setColorAt(0, QColor(207, 216, 228, 255))
            gradient.setColorAt(1, QColor(207, 216, 228, 0))
        else:
            gradient.setColorAt(0, QColor(0, 0, 0, 255))
            gradient.setColorAt(1, QColor(0, 0, 0, 0))

        painter.fillPath(path, QBrush(gradient))

        # # 绘制图片
        pixmap = self.banner.scaled(self.size(), aspectMode=Qt.AspectRatioMode.KeepAspectRatioByExpanding, mode=Qt.TransformationMode.SmoothTransformation)
        painter.fillPath(path, QBrush(pixmap))


class HomeInterface(ScrollArea):
    """Home interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)

        self.banner = BannerWidget(self)
        self.view = QWidget(self)
        self.vBoxLayout = QVBoxLayout(self.view)
        self.taskCheckboxGroup = CheckboxGroup(self.view)
        self.residentActivityWorker = None
        self.rewardWorker = None
        self.__initWidget()
        self.loadSamples()

    def __initWidget(self):
        self.view.setObjectName("view")
        self.setObjectName("HomeInterface")
        StyleSheet.HOME_INTERFACE.apply(self)

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(self.view)
        self.setWidgetResizable(True)

        self.vBoxLayout.setContentsMargins(0, 0, 0, 36)
        self.vBoxLayout.setSpacing(10)
        self.vBoxLayout.addWidget(self.banner)
        self.vBoxLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

    def loadSamples(self):
        """load samples"""

        tipBar = InfoBar(
            icon=InfoBarIcon.WARNING,
            title=self.tr("Warning"),
            content="推荐使用 MUMU模拟器 分辨率必须为16:9，推荐: 1920x1080/1280x720",
            orient=Qt.Orientation.Vertical,
            isClosable=False,
            duration=-1,
            position=InfoBarPosition.NONE,
            parent=self.view,
        )

        basicInputView = ButtonCardView(
            "开始运行", header=self.taskCheckboxGroup, parent=self.view
        )

        basicInputView.vBoxLayout.insertWidget(0, tipBar)
        rewardSelector = QWidget(self.view)
        rewardLayout = QVBoxLayout(rewardSelector)
        rewardLayout.setContentsMargins(0, 0, 0, 4)
        rewardLayout.setSpacing(8)

        fullRealmTitle = QLabel("全境特供奖励（每天 3 次）", rewardSelector)
        fullRealmTitle.setStyleSheet("font-size: 16px; font-weight: 600;")
        rewardLayout.addWidget(fullRealmTitle)
        fullRealmGrid = QGridLayout()
        fullRealmGrid.setSpacing(8)
        self.fullRealmRewardCards = {}
        full_realm_icons = {
            "学会装备箱": "academy_lost_chest.png",
            "黑月装备箱": "blackmoon_lost_chest.png",
            "帝国装备箱": "empire_lost_chest.png",
        }
        for column, (reward, stage) in enumerate(FULL_REALM_REWARDS.items()):
            card = RewardChoiceCard(
                reward,
                reward,
                f"全境特供 · {stage}",
                full_realm_icons[reward],
                rewardSelector,
            )
            card.selected.connect(self.selectFullRealmReward)
            fullRealmGrid.addWidget(card, 0, column)
            self.fullRealmRewardCards[reward] = card
        rewardLayout.addLayout(fullRealmGrid)

        siegeTitle = QLabel("剩余澄清度刷取（按主要掉落选择）", rewardSelector)
        siegeTitle.setStyleSheet("font-size: 16px; font-weight: 600; margin-top: 6px;")
        rewardLayout.addWidget(siegeTitle)
        siegeGrid = QGridLayout()
        siegeGrid.setSpacing(8)
        self.siegeRewardCards = {}
        for index, task in enumerate(SIEGE_TASKS):
            reward, icon_name = SIEGE_REWARDS[task]
            card = RewardChoiceCard(
                task, reward, f"利刃围剿 · {task}", icon_name, rewardSelector
            )
            card.selected.connect(self.selectSiegeTask)
            siegeGrid.addWidget(card, index // 3, index % 3)
            self.siegeRewardCards[task] = card
        rewardLayout.addLayout(siegeGrid)
        basicInputView.vBoxLayout.insertWidget(1, rewardSelector)
        self.selectFullRealmReward(cfg.residentActivityFullRealmReward.value)
        self.selectSiegeTask(cfg.residentActivityTask.value)

        autoRewardSelector = QWidget(self.view)
        autoRewardLayout = QHBoxLayout(autoRewardSelector)
        autoRewardLayout.setContentsMargins(0, 0, 0, 0)
        autoRewardLayout.addWidget(QLabel("活动结束后自动领取", autoRewardSelector))
        autoRewardLayout.addWidget(QLabel("每日活跃", autoRewardSelector))
        self.dailyRewardSwitch = SwitchButton(autoRewardSelector)
        self.dailyRewardSwitch.setChecked(bool(cfg.autoCollectDailyActivity.value))
        self.dailyRewardSwitch.checkedChanged.connect(
            lambda value: qconfig.set(cfg.autoCollectDailyActivity, value)
        )
        autoRewardLayout.addWidget(self.dailyRewardSwitch)
        autoRewardLayout.addWidget(QLabel("环游手册", autoRewardSelector))
        self.manualRewardSwitch = SwitchButton(autoRewardSelector)
        self.manualRewardSwitch.setChecked(bool(cfg.autoCollectTravelManual.value))
        self.manualRewardSwitch.checkedChanged.connect(
            lambda value: qconfig.set(cfg.autoCollectTravelManual, value)
        )
        autoRewardLayout.addWidget(self.manualRewardSwitch)
        autoRewardLayout.addStretch(1)
        basicInputView.vBoxLayout.insertWidget(2, autoRewardSelector)
        # self.taskCheckboxGroup.addCheckbox("购买桦石", cfg.huashi)
        # self.taskCheckboxGroup.addCheckbox("刷铁安局", cfg.railwaySafetyBureau)

        basicInputView.addSampleCard(
            icon=FluentIcon.ACCEPT,
            title="领取奖励",
            content="按上方开关领取每日活跃与环游手册奖励",
            func=self.startRewardCollection,
            routekey="LoggerInterface",
        )

        basicInputView.addSampleCard(
            icon=FluentIcon.PLAY,
            title="全域整备",
            content="私贩追缴、全境特供及所选利刃围剿任务",
            func=self.startResidentActivity,
            routekey="LoggerInterface",
        )

        basicInputView.addSampleCard(
            icon=FluentIcon.ACCEPT,
            title="单次扫荡验证",
            content="仅对所选利刃围剿任务扫荡一次",
            func=self.startResidentActivityOnce,
            routekey="LoggerInterface",
        )

        basicInputView.addSampleCard(
            icon=":/gallery/images/controls/Button.png",
            title="停止",
            content="停止运行",
            func=stop,
            routekey="LoggerInterface",
        )


        self.vBoxLayout.addWidget(basicInputView)

    def startResidentActivity(self):
        if self.residentActivityWorker and self.residentActivityWorker.isRunning():
            return
        self.residentActivityWorker = Worker(
            run_resident_activity_with_rewards,
            task=cfg.residentActivityTask.value,
            full_realm_reward=cfg.residentActivityFullRealmReward.value,
            daily_activity=bool(cfg.autoCollectDailyActivity.value),
            travel_manual=bool(cfg.autoCollectTravelManual.value),
        )
        self.residentActivityWorker.start()

    def selectFullRealmReward(self, reward):
        qconfig.set(cfg.residentActivityFullRealmReward, reward)
        for value, card in self.fullRealmRewardCards.items():
            card.setChecked(value == reward)

    def selectSiegeTask(self, task):
        qconfig.set(cfg.residentActivityTask, task)
        for value, card in self.siegeRewardCards.items():
            card.setChecked(value == task)

    def startResidentActivityOnce(self):
        if self.residentActivityWorker and self.residentActivityWorker.isRunning():
            return
        self.residentActivityWorker = Worker(
            run_resident_activity_once,
            task=cfg.residentActivityTask.value,
        )
        self.residentActivityWorker.start()

    def startRewardCollection(self):
        if self.rewardWorker and self.rewardWorker.isRunning():
            return
        daily = bool(cfg.autoCollectDailyActivity.value)
        manual = bool(cfg.autoCollectTravelManual.value)
        if not daily and not manual:
            return
        self.rewardWorker = Worker(
            collect_rewards,
            daily_activity=daily,
            travel_manual=manual,
        )
        self.rewardWorker.start()
