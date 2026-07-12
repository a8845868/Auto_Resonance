from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QGraphicsItem, QGraphicsRectItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView, QWidget, QVBoxLayout

from app.utils.constants import ROOT_PATH


class FurnitureBlockItem(QGraphicsRectItem):
    def __init__(self, slot: dict, catalog_item: dict, placed: int, owned: int, editable: bool, parent=None):
        super().__init__(0, 0, float(slot["w"]), float(slot["h"]), parent)
        self.slot = slot
        self.catalog_item = catalog_item
        self.placed = max(0, int(placed))
        self.owned = max(0, int(owned))
        self.editable = editable
        self.press_position = QPointF()
        self.text = QGraphicsTextItem(self)
        self.text.setDefaultTextColor(QColor("#f4f4f4"))
        self.text.setTextWidth(max(60, float(slot["w"]) - 12))
        self.text.setPos(6, 4)
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        self.text.setFont(font)
        self.text.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setPos(float(slot["x"]), float(slot["y"]))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, editable)
        self.setCursor(Qt.CursorShape.OpenHandCursor if editable else Qt.CursorShape.PointingHandCursor)
        self.refresh_style()

    def refresh_style(self):
        required = max(0, int(self.slot.get("count", 0)))
        if required == 0:
            fill, border = QColor("#455a64"), QColor("#90a4ae")
        elif self.placed >= required:
            fill, border = QColor("#147d64"), QColor("#45e0bd")
        elif self.placed > 0:
            fill, border = QColor("#8a6518"), QColor("#ffc857")
        elif self.owned > 0:
            fill, border = QColor("#285b82"), QColor("#62b5f3")
        else:
            fill, border = QColor("#3d4147"), QColor("#777d86")
        self.setBrush(QBrush(fill))
        self.setPen(QPen(border, 2))
        label = self.slot.get("label") or self.catalog_item.get("name", self.slot.get("item", "未知家具"))
        self.text.setPlainText(f"{label} ×{required}\n摆放 {self.placed} / 仓库 {self.owned}")
        self.setToolTip(
            f"{label}\n需要 {required}，仓库 {self.owned}，当前结构摆放 {self.placed}\n"
            f"获取：{self.catalog_item.get('source', '未收录')}\n单击点亮/置灰；自定义结构可拖动。"
        )

    def mousePressEvent(self, event):
        self.press_position = event.screenPos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        moved = (event.screenPos() - self.press_position).manhattanLength() > QApplication.startDragDistance()
        super().mouseReleaseEvent(event)
        scene = self.scene()
        if moved:
            if hasattr(scene, "editor"):
                scene.editor.blockMoved.emit(self.slot["id"], self.pos().x(), self.pos().y())
        elif event.button() == Qt.MouseButton.LeftButton and hasattr(scene, "editor"):
            scene.editor.blockClicked.emit(self.slot["id"])


class PassengerLayoutEditor(QWidget):
    blockClicked = Signal(str)
    blockMoved = Signal(str, float, float)
    selectionChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scene = QGraphicsScene(self)
        self.scene.editor = self
        self.view = QGraphicsView(self.scene, self)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setMinimumHeight(520)
        self.view.setStyleSheet("QGraphicsView { background:#202225; border:1px solid #45494f; border-radius:6px; }")
        layout.addWidget(self.view)
        self.scene.selectionChanged.connect(self._selectionChanged)
        self.items_by_id: dict[str, FurnitureBlockItem] = {}

    def set_layout(self, diagram: dict, catalog: dict[str, dict], inventory: dict[str, int], placements: dict[str, int]):
        self.scene.clear()
        self.items_by_id.clear()
        canvas = diagram.get("canvas", {"width": 1600, "height": 820})
        self.scene.setSceneRect(0, 0, float(canvas["width"]), float(canvas["height"]))
        background_path = diagram.get("background")
        if background_path:
            pixmap = QPixmap(str(ROOT_PATH / background_path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    int(canvas["width"]), int(canvas["height"]),
                    Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
                )
                background = self.scene.addPixmap(scaled)
                background.setOpacity(0.16)
                background.setZValue(-30)
        for zone in diagram.get("zones", []):
            rect = self.scene.addRect(
                float(zone["x"]), float(zone["y"]), float(zone["w"]), float(zone["h"]),
                QPen(QColor("#6e737c"), 2), QBrush(QColor(41, 44, 49, 205)),
            )
            rect.setZValue(-10)
            title = self.scene.addText(zone["name"])
            title.setDefaultTextColor(QColor("#d5d8dc"))
            title.setPos(float(zone["x"]) + 8, float(zone["y"]) + 2)
            title.setZValue(-9)
        editable = bool(diagram.get("editable", False))
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag if editable else QGraphicsView.DragMode.ScrollHandDrag)
        for slot in diagram.get("slots", []):
            item_data = catalog.get(slot["item"], {"name": slot["item"], "source": "未收录"})
            block = FurnitureBlockItem(
                slot,
                item_data,
                placements.get(slot["id"], 0),
                inventory.get(slot["item"], 0),
                editable,
            )
            self.scene.addItem(block)
            self.items_by_id[slot["id"]] = block

    def zoom(self, factor: float):
        self.view.resetTransform()
        self.view.scale(factor, factor)

    def selected_slot_id(self) -> str | None:
        selected = [item for item in self.scene.selectedItems() if isinstance(item, FurnitureBlockItem)]
        return selected[0].slot["id"] if selected else None

    def _selectionChanged(self):
        self.selectionChanged.emit(self.selected_slot_id() or "")
