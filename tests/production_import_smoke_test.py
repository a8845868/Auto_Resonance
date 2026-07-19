"""Production import-chain smoke tests for unattended runtime entry points."""


def test_production_runtime_import_chain_loads_without_suppression():
    from app.view import dashboard_interface
    from auto.fatigue_recovery import run_daily_fatigue_recovery
    from auto.resident_activity import (
        ResidentActivityAutomation,
        run_resident_activity,
    )
    from auto.reward_collection import RewardCollector
    from auto.run_business.main import run_with_recovery

    assert dashboard_interface.run_resident_activity is run_resident_activity
    assert callable(ResidentActivityAutomation)
    assert callable(run_daily_fatigue_recovery)
    assert callable(RewardCollector)
    assert callable(run_with_recovery)

