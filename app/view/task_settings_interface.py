"""Task-specific configuration pages, following the ALAS task-page layout."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    ScrollArea,
    SwitchSettingCard,
    qconfig,
)

from app.common.config import cfg
from app.common.style_sheet import StyleSheet
from app.components.task_schedule_card import TaskScheduleCard
from auto.resident_activity import FULL_REALM_REWARDS, SIEGE_REWARDS, SIEGE_TASKS
from core.services.fatigue_planner import fatigue_plan_lines, next_fatigue_refresh
from core.services.task_schedule_state import task_timing


REWARD_ICON_DIR = Path(__file__).resolve().parents[2] / "resources" / "rewards"


class RewardChoiceCard(QWidget):
    selected = Signal(str)

    def __init__(self, value, title, subtitle, icon_name, parent=None):
        super().__init__(parent)
        self.value = value
        self.setObjectName("rewardChoiceCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(92)
        icon = QLabel(self)
        icon.setPixmap(QPixmap(str(REWARD_ICON_DIR / icon_name)).scaled(
            56, 56, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        icon.setFixedSize(60, 60)
        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        subtitle_label = QLabel(subtitle, self)
        subtitle_label.setStyleSheet("font-size: 11px; color: #7a7a7a;")
        self.selectionLabel = QLabel("✓  当前选择", self)
        self.selectionLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.selectionLabel.setFixedSize(78, 24)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(3)
        text_layout.addWidget(title_label)
        text_layout.addWidget(subtitle_label)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addWidget(icon)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.selectionLabel, 0, Qt.AlignmentFlag.AlignTop)
        self.setChecked(False)

    def setChecked(self, checked):
        border = "#43a5ff" if checked else "rgba(128,128,128,0.24)"
        background = "rgba(30,120,210,0.28)" if checked else "rgba(128,128,128,0.05)"
        self.setStyleSheet(
            f"QWidget#rewardChoiceCard {{ border: 2px solid {border}; border-radius: 10px; "
            f"background-color: {background}; }}"
            "QWidget#rewardChoiceCard QLabel { border: none; background: transparent; }"
        )
        self.selectionLabel.setVisible(checked)
        self.selectionLabel.setStyleSheet(
            "color: white; font-size: 11px; font-weight: 600; "
            "border-radius: 12px; background-color: #1688e8;"
        )

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.selected.emit(self.value)


class TaskSettingsPage(ScrollArea):
    def __init__(self, title: str, object_name: str, parent=None):
        super().__init__(parent)
        self.scrollWidget = QWidget(self)
        self.layout = QVBoxLayout(self.scrollWidget)
        self.titleLabel = QLabel(title, self)
        self.setObjectName(object_name)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setViewportMargins(0, 72, 0, 20)
        self.scrollWidget.setObjectName("scrollWidget")
        self.titleLabel.setObjectName("titleLabel")
        self.titleLabel.move(36, 26)
        self.layout.setContentsMargins(36, 0, 36, 20)
        self.layout.setSpacing(12)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        StyleSheet.VIEW_INTERFACE.apply(self)


class RewardCollectionInterface(TaskSettingsPage):
    def __init__(self, parent=None):
        super().__init__("领取任务奖励", "RewardCollectionInterface", parent)
        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.ACCEPT,
            "加入任务序列",
            "领取任务奖励",
            cfg.enableRewardCollection,
            self.scrollWidget,
        ))
        self.scheduleCard = TaskScheduleCard("reward_collection", self.scrollWidget)
        self.layout.addWidget(self.scheduleCard)

        strategy_row = QWidget(self.scrollWidget)
        strategy_layout = QHBoxLayout(strategy_row)
        strategy_layout.setContentsMargins(12, 8, 12, 8)
        strategy_layout.addWidget(QLabel("领取策略", strategy_row))
        self.strategyCombo = QComboBox(strategy_row)
        self.strategyCombo.addItem("尽可能完成后领取", "maximize_progress")
        self.strategyCombo.addItem("仅领取当前已解锁奖励", "claim_only")
        index = self.strategyCombo.findData(str(cfg.rewardStrategy.value))
        self.strategyCombo.setCurrentIndex(max(0, index))
        self.strategyCombo.currentIndexChanged.connect(
            lambda _index: qconfig.set(
                cfg.rewardStrategy, self.strategyCombo.currentData()
            )
        )
        strategy_layout.addWidget(self.strategyCombo, 1)
        self.layout.addWidget(strategy_row)
        self.rewardStatusLabel = QLabel(self.scrollWidget)
        self.rewardStatusLabel.setWordWrap(True)
        self.rewardStatusLabel.setStyleSheet(
            "padding: 10px; border-radius: 8px; background: rgba(128,128,128,.06);"
        )
        self.layout.addWidget(self.rewardStatusLabel)

        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.CALENDAR,
            "领取每日活跃奖励",
            "每日活跃",
            cfg.autoCollectDailyActivity,
            self.scrollWidget,
        ))
        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.BOOK_SHELF,
            "领取环游手册奖励",
            "环游手册",
            cfg.autoCollectTravelManual,
            self.scrollWidget,
        ))

    def updateRewardStatus(self):
        timing = task_timing("reward_collection")
        result = timing.get("result") if isinstance(timing.get("result"), dict) else {}
        snapshot = result.get("snapshot_after") if isinstance(result.get("snapshot_after"), dict) else {}
        activity = (
            f"{snapshot.get('daily_activity_current')}/{snapshot.get('daily_activity_max')}"
            if snapshot.get("daily_activity_current") is not None
            else "未知"
        )
        handbook = (
            f"{snapshot.get('handbook_daily_tasks_completed')}/{snapshot.get('handbook_daily_tasks_total')}"
            if snapshot.get("handbook_daily_tasks_completed") is not None
            else "未知"
        )
        blockers = "、".join(result.get("blocked_reasons") or []) or "无"
        self.rewardStatusLabel.setText(
            f"当前状态：{result.get('status') or '尚未检查'} · 每日活跃 {activity} · "
            f"手册每日任务 {handbook}\n当前待办/阻塞：{blockers} · "
            f"下一次原因：{result.get('next_run_reason') or '未记录'}"
        )

    def showEvent(self, event):
        self.updateRewardStatus()
        super().showEvent(event)


class FatiguePlannerInterface(TaskSettingsPage):
    def __init__(self, parent=None):
        super().__init__("疲劳规划", "FatiguePlannerInterface", parent)
        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.CAFE,
            "加入任务序列",
            "每日 05:00 先恢复再跑商；12:00、18:00 复查新发便当",
            cfg.enableFatiguePlanner,
            self.scrollWidget,
        ))
        self.scheduleCard = TaskScheduleCard("fatigue_recovery", self.scrollWidget)
        self.layout.addWidget(self.scheduleCard)
        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.CAFE,
            "使用银枝继续喝气泡水",
            "关闭时只使用免费次数和 500 铁盟币气泡水",
            cfg.UseSilverBranch,
            self.scrollWidget,
        ))

        self.planPanel = QWidget(self.scrollWidget)
        self.planPanel.setObjectName("fatiguePlanPanel")
        self.planPanel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.planPanel.setStyleSheet(
            "QWidget#fatiguePlanPanel { background-color: rgba(30,120,210,0.14); "
            "border: 1px solid rgba(67,165,255,0.65); border-radius: 12px; }"
            "QWidget#fatiguePlanPanel QLabel { border: none; background: transparent; }"
        )
        panel_layout = QVBoxLayout(self.planPanel)
        panel_layout.setContentsMargins(18, 14, 18, 14)
        title = QLabel("每日执行规则", self.planPanel)
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.planSummaryLabel = QLabel(self.planPanel)
        self.planSummaryLabel.setWordWrap(True)
        self.planSummaryLabel.setStyleSheet("font-size: 13px; line-height: 1.5;")
        panel_layout.addWidget(title)
        panel_layout.addWidget(self.planSummaryLabel)
        self.layout.addWidget(self.planPanel)
        self.updatePlanSummary()

    def updatePlanSummary(self):
        lines = list(
            fatigue_plan_lines(bool(cfg.UseSilverBranch.value))
        )
        timing = task_timing("fatigue_recovery")
        result = timing.get("result") if isinstance(timing.get("result"), dict) else {}
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        if plan:
            snapshot = plan.get("snapshot") or {}
            actions = plan.get("immediate_actions") or []
            lines.extend(
                [
                    f"最近观测：疲劳 {snapshot.get('fatigue_used', '未知')}/"
                    f"{snapshot.get('fatigue_cap', '未知')}，站点 {snapshot.get('current_station_id', '未知')}",
                    f"最近规划：{plan.get('status', 'UNKNOWN')}；立即动作 "
                    f"{', '.join(action.get('kind', '') for action in actions) or '无'}；"
                    f"下一触发 {plan.get('next_trigger') or '未记录'}",
                ]
            )
        self.planSummaryLabel.setText(
            "\n".join(
                f"{index}. {line}"
                for index, line in enumerate(lines, start=1)
            )
        )

    def buildQueuedTask(self):
        if not bool(cfg.enableFatiguePlanner.value):
            return None
        from app.utils.task_queue import QueuedTask
        from auto.fatigue_recovery import run_daily_fatigue_recovery

        return QueuedTask(
            "疲劳规划",
            run_daily_fatigue_recovery,
            key="fatigue_recovery",
            next_run_factory=next_fatigue_refresh,
        )

    def showEvent(self, event):
        self.updatePlanSummary()
        super().showEvent(event)

class ResidentActivityInterface(TaskSettingsPage):
    def __init__(self, parent=None):
        super().__init__("扫荡配置", "ResidentActivityInterface", parent)
        self.layout.addWidget(SwitchSettingCard(
            FluentIcon.PLAY,
            "加入任务序列",
            "全域整备与剩余澄清度扫荡",
            cfg.enableResidentActivity,
            self.scrollWidget,
        ))
        self.scheduleCard = TaskScheduleCard("resident_activity", self.scrollWidget)
        self.layout.addWidget(self.scheduleCard)

        self.planPanel = QWidget(self.scrollWidget)
        self.planPanel.setObjectName("currentPlanPanel")
        self.planPanel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.planPanel.setStyleSheet(
            "QWidget#currentPlanPanel { background-color: rgba(30,120,210,0.14); "
            "border: 1px solid rgba(67,165,255,0.65); border-radius: 12px; }"
            "QWidget#currentPlanPanel QLabel { border: none; background: transparent; }"
        )
        plan_layout = QVBoxLayout(self.planPanel)
        plan_layout.setContentsMargins(18, 14, 18, 14)
        plan_header = QHBoxLayout()
        plan_title = QLabel("当前扫荡方案", self.planPanel)
        plan_title.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.runStateLabel = QLabel("●  等待开始", self.planPanel)
        self.setRunState("●  等待开始", "#8fd694")
        plan_header.addWidget(plan_title)
        plan_header.addStretch(1)
        plan_header.addWidget(self.runStateLabel)
        plan_layout.addLayout(plan_header)
        self.planSummaryLabel = QLabel(self.planPanel)
        self.planSummaryLabel.setWordWrap(True)
        self.planSummaryLabel.setStyleSheet("font-size: 13px; line-height: 1.5;")
        plan_layout.addWidget(self.planSummaryLabel)
        self.layout.addWidget(self.planPanel)

        full_title = QLabel("全境特供奖励（每天 3 次）", self.scrollWidget)
        full_title.setStyleSheet("font-size: 16px; font-weight: 600; margin-top: 12px;")
        self.layout.addWidget(full_title)
        full_grid = QGridLayout()
        full_grid.setSpacing(8)
        self.fullRealmRewardCards = {}
        icons = {
            "学会装备箱": "academy_lost_chest.png",
            "黑月装备箱": "blackmoon_lost_chest.png",
            "帝国装备箱": "empire_lost_chest.png",
        }
        for column, (reward, stage) in enumerate(FULL_REALM_REWARDS.items()):
            card = RewardChoiceCard(reward, reward, f"全境特供 · {stage}", icons[reward], self.scrollWidget)
            card.selected.connect(self.selectFullRealmReward)
            full_grid.addWidget(card, 0, column)
            self.fullRealmRewardCards[reward] = card
        self.layout.addLayout(full_grid)

        siege_title = QLabel("剩余澄清度刷取（按主要掉落选择）", self.scrollWidget)
        siege_title.setStyleSheet("font-size: 16px; font-weight: 600; margin-top: 12px;")
        self.layout.addWidget(siege_title)
        siege_grid = QGridLayout()
        siege_grid.setSpacing(8)
        self.siegeRewardCards = {}
        for index, task in enumerate(SIEGE_TASKS):
            reward, icon_name = SIEGE_REWARDS[task]
            card = RewardChoiceCard(task, reward, f"利刃围剿 · {task}", icon_name, self.scrollWidget)
            card.selected.connect(self.selectSiegeTask)
            siege_grid.addWidget(card, index // 3, index % 3)
            self.siegeRewardCards[task] = card
        self.layout.addLayout(siege_grid)
        self.selectFullRealmReward(cfg.residentActivityFullRealmReward.value)
        self.selectSiegeTask(cfg.residentActivityTask.value)
        self.updateCurrentPlan()

    def selectFullRealmReward(self, reward):
        qconfig.set(cfg.residentActivityFullRealmReward, reward)
        for value, card in self.fullRealmRewardCards.items():
            card.setChecked(value == reward)
        self.updateCurrentPlan()

    def selectSiegeTask(self, task):
        qconfig.set(cfg.residentActivityTask, task)
        for value, card in self.siegeRewardCards.items():
            card.setChecked(value == task)
        self.updateCurrentPlan()

    def updateCurrentPlan(self):
        if not hasattr(self, "planSummaryLabel"):
            return
        reward = cfg.residentActivityFullRealmReward.value
        stage = FULL_REALM_REWARDS[reward]
        task = cfg.residentActivityTask.value
        main_drop = SIEGE_REWARDS[task][0]
        reward_actions = []
        if bool(cfg.enableRewardCollection.value):
            if bool(cfg.autoCollectDailyActivity.value):
                reward_actions.append("每日活跃奖励")
            if bool(cfg.autoCollectTravelManual.value):
                reward_actions.append("环游手册奖励")
        reward_text = "、".join(reward_actions) if reward_actions else "未启用奖励领取任务"
        self.planSummaryLabel.setText(
            "① 私贩追缴：补齐本周剩余奖励次数（每周上限 3 次）\n"
            f"② 全境特供：刷取【{reward}】— {stage}，补齐今日剩余次数（每天上限 3 次）\n"
            f"③ 利刃围剿：刷取【{main_drop}】— {task}，持续到澄清度不足\n"
            f"④ 任务奖励：{reward_text}"
        )

    def setRunState(self, text, color):
        if hasattr(self, "runStateLabel"):
            self.runStateLabel.setText(text)
            self.runStateLabel.setStyleSheet(
                f"color: {color}; font-size: 13px; font-weight: 600;"
            )

    def showEvent(self, event):
        self.updateCurrentPlan()
        super().showEvent(event)
