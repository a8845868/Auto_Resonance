"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-10 22:54:08
LastEditTime: 2025-02-05 18:42:29
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

from functools import partial

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea

from app.common.config import cfg, qconfig
from app.common.style_sheet import StyleSheet
from app.components.button_card import ButtonCardView
from app.utils.worker import Worker
from core.control.adb_port import EmulatorInfo, get_adb_port
from core.model.config import config


class ADBDataInterface(ScrollArea):
    """ADB端口信息扫描 interface"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.scrollWidget = QWidget(self)

        self.vBoxLayout = QVBoxLayout(self.scrollWidget)
        self.scanWorker = None

        self.__initWidget()

    def __initWidget(self):
        self.setViewportMargins(0, 20, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName("ADBDataInterface")

        # initialize style sheet
        self.scrollWidget.setObjectName("scrollWidget")
        StyleSheet.SETTING_INTERFACE.apply(self)

        # initialize layout
        self.loadSamples()

    def showEvent(self, event):
        """当切换到该页面时，触发这个事件"""
        super().showEvent(event)
        self.basicInputView.removeAllSampleCards()
        self.basicInputView.set_title("加载中...")
        QTimer.singleShot(100, self.start_port_scan)

    def start_port_scan(self):
        """动画结束后调用的方法"""
        if self.scanWorker is not None:
            return
        worker = Worker(get_adb_port, preferred=cfg.device.value)
        self.scanWorker = worker
        worker.result.connect(self.update_adb)
        worker.finished.connect(lambda: self._scanFinished(worker))
        worker.start()

    def _scanFinished(self, worker):
        if worker is self.scanWorker:
            self.scanWorker = None
        worker.deleteLater()

    def shutdown(self) -> bool:
        """Keep the page alive until its read-only discovery worker exits."""

        return self.scanWorker is None

    def loadSamples(self):
        """load samples"""
        self.basicInputView = ButtonCardView("加载中...", parent=self.scrollWidget)

        self.vBoxLayout.addWidget(self.basicInputView)

    def update_adb(self, info_list: list[EmulatorInfo]):
        self.basicInputView.set_title("ADB信息")
        for info in info_list:
            status = f"127.0.0.1:{info.port}" if info.port else "未启动"
            content = f"{status} · {info.type.value} · 多开索引 {info.index}"
            self.basicInputView.addSampleCard(
                icon=":/gallery/images/controls/Button.png",
                title=info.name,
                content=content,
                func=partial(self.set_port, info),
            )

    def set_port(self, info: EmulatorInfo):
        qconfig.set(cfg.device, info)
