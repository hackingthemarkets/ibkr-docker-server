from datetime import UTC, datetime

from scripts.gateway_readiness import build_report
from scripts.gateway_watchdog import WatchdogState


def test_ready_report():
    ready, report = build_report(
        now=datetime(2026, 11, 8, 14, 0, tzinfo=UTC),
        probe_healthy=True,
        probe_detail="all APIs healthy",
        service_active=True,
        disk_percent=31.2,
        state=WatchdogState(status="HEALTHY", operator_required=False),
    )

    assert ready
    assert "Sunday readiness: READY" in report
    assert "Disk usage: 31.2%" in report


def test_operator_required_blocks_readiness():
    ready, report = build_report(
        now=datetime(2026, 11, 8, 14, 0, tzinfo=UTC),
        probe_healthy=True,
        probe_detail="all APIs healthy",
        service_active=True,
        disk_percent=31.2,
        state=WatchdogState(status="OPERATOR_REQUIRED", operator_required=True),
    )

    assert not ready
    assert "Sunday readiness: ACTION REQUIRED" in report
