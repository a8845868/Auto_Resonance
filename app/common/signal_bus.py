from PySide6.QtCore import QObject, Signal


class SignalBus(QObject):
    """Signal bus"""

    switchToCard = Signal(str)
    bookBudgetChanged = Signal()


signalBus = SignalBus()
