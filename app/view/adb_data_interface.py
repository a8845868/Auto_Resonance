"""
Author: Night-stars-1 nujj1042633805@gmail.com
Date: 2024-04-10 22:54:08
LastEditTime: 2025-02-05 18:42:29
LastEditors: Night-stars-1 nujj1042633805@gmail.com
"""

from functools import partial

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ScrollArea

from app.common.config import cfg, qconfig
from app.common.style_sheet import StyleSheet
from app.components.button_card import ButtonCardView
from app.utils.worker import Worker
from core.control.adb_port import (
    EmulatorInfo,
    EmulatorType,
    get_adb_port,
    resolve_adb_executable,
)
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
        self.connectionGroup = QGroupBox("设备连接", self.scrollWidget)
        form = QFormLayout(self.connectionGroup)

        self.emulatorTypeCombo = QComboBox(self.connectionGroup)
        self.emulatorTypeCombo.addItem("MuMu V5", EmulatorType.MUMUV5)
        self.emulatorTypeCombo.addItem("MuMu V4", EmulatorType.MUMUV4)
        self.emulatorTypeCombo.addItem("自定义 ADB", EmulatorType.CUSTOM)

        self.installPathEdit = QLineEdit(self.connectionGroup)
        install_row = QHBoxLayout()
        install_row.addWidget(self.installPathEdit)
        install_button = QPushButton("选择目录", self.connectionGroup)
        install_button.clicked.connect(self.choose_install_directory)
        install_row.addWidget(install_button)

        self.adbPathEdit = QLineEdit(self.connectionGroup)
        adb_row = QHBoxLayout()
        adb_row.addWidget(self.adbPathEdit)
        adb_button = QPushButton("选择 adb.exe", self.connectionGroup)
        adb_button.clicked.connect(self.choose_adb_executable)
        adb_row.addWidget(adb_button)

        self.instanceSpin = QSpinBox(self.connectionGroup)
        self.instanceSpin.setRange(0, 9999)
        self.adbHostEdit = QLineEdit(self.connectionGroup)
        self.portSpin = QSpinBox(self.connectionGroup)
        self.portSpin.setRange(0, 65535)
        self.portSpin.setSpecialValueText("自动解析")

        save_button = QPushButton("保存设备连接配置", self.connectionGroup)
        save_button.clicked.connect(self.save_connection_configuration)

        form.addRow("模拟器类型", self.emulatorTypeCombo)
        form.addRow("模拟器安装目录", install_row)
        form.addRow("ADB 程序", adb_row)
        form.addRow("多开实例 ID", self.instanceSpin)
        form.addRow("ADB Host", self.adbHostEdit)
        form.addRow("ADB Port", self.portSpin)
        form.addRow("", save_button)
        self.vBoxLayout.addWidget(self.connectionGroup)

        self.basicInputView = ButtonCardView("加载中...", parent=self.scrollWidget)
        self.vBoxLayout.addWidget(self.basicInputView)
        self.load_connection_configuration()

    def load_connection_configuration(self):
        info: EmulatorInfo = cfg.device.value
        combo_index = self.emulatorTypeCombo.findData(info.type)
        self.emulatorTypeCombo.setCurrentIndex(max(0, combo_index))
        self.installPathEdit.setText(str(info.path or ""))
        self.adbPathEdit.setText(str(info.adb_path or ""))
        self.instanceSpin.setValue(int(info.index))
        self.adbHostEdit.setText(str(info.adb_host or "127.0.0.1"))
        self.portSpin.setValue(int(info.port or 0))

    def choose_install_directory(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择模拟器安装目录",
            self.installPathEdit.text(),
        )
        if not selected:
            return
        self.installPathEdit.setText(selected)
        adb = resolve_adb_executable(self._connection_info())
        if adb is not None:
            self.adbPathEdit.setText(str(adb))

    def choose_adb_executable(self):
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "选择 ADB 程序",
            self.adbPathEdit.text() or self.installPathEdit.text(),
            "ADB executable (adb.exe);;Executable files (*.exe)",
        )
        if selected:
            self.adbPathEdit.setText(selected)

    def _connection_info(self) -> EmulatorInfo:
        emulator_type = self.emulatorTypeCombo.currentData()
        return EmulatorInfo(
            name=(
                f"MuMu 多开 {self.instanceSpin.value()}"
                if emulator_type is not EmulatorType.CUSTOM
                else "自定义 ADB"
            ),
            port=self.portSpin.value() or None,
            path=self.installPathEdit.text().strip(),
            type=emulator_type,
            index=self.instanceSpin.value(),
            adb_path=self.adbPathEdit.text().strip(),
            adb_host=self.adbHostEdit.text().strip() or "127.0.0.1",
        )

    def save_connection_configuration(self):
        qconfig.set(cfg.device, self._connection_info())

    def update_adb(self, info_list: list[EmulatorInfo]):
        self.basicInputView.set_title("ADB信息")
        for info in info_list:
            status = f"{info.adb_host}:{info.port}" if info.port else "未启动"
            content = f"{status} · {info.type.value} · 多开索引 {info.index}"
            self.basicInputView.addSampleCard(
                icon=":/gallery/images/controls/Button.png",
                title=info.name,
                content=content,
                func=partial(self.set_port, info),
            )

    def set_port(self, info: EmulatorInfo):
        current: EmulatorInfo = cfg.device.value
        if (
            current.adb_path
            and current.type is info.type
            and current.path == info.path
        ):
            info.adb_path = current.adb_path
        qconfig.set(cfg.device, info)
        refresh = getattr(self, "load_connection_configuration", None)
        if callable(refresh):
            refresh()
