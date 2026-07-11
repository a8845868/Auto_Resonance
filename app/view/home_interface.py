"""Backward-compatible import for the scheduler dashboard."""

from app.view.dashboard_interface import DashboardInterface


HomeInterface = DashboardInterface

__all__ = ["HomeInterface"]
