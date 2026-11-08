import sys
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.gateway_watchdog import (
    GatewayProbe,
    GatewayRestarter,
    ProbeResult,
    Watchdog,
    WatchdogConfig,
    WatchdogState,
    is_weekend_quiet,
    is_within_window,
    restart_decision,
)


def configuration(tmp_path: Path, **overrides) -> WatchdogConfig:
    values = {
        "host": "127.0.0.1",
        "port": 4002,
        "client_id": 997,
        "probe_interval_seconds": 60,
        "probe_timeout_seconds": 10,
        "failure_threshold": 5,
        "maintenance_start": time(23, 50),
        "maintenance_end": time(2, 5),
        "weekend_quiet_end_sunday": time(8),
        "minimum_automatic_restart_interval_seconds": 86400,
        "restart_command_timeout_seconds": 60,
        "recovery_timeout_seconds": 180,
        "recovery_probe_interval_seconds": 10,
        "state_path": tmp_path / "state.json",
        "restart_command": ("true",),
        "timezone": ZoneInfo("America/New_York"),
    }
    values.update(overrides)
    return WatchdogConfig(**values)


def test_maintenance_window_spans_midnight():
    assert is_within_window(
        datetime(2026, 9, 8, 23, 59, tzinfo=ZoneInfo("America/New_York")),
        time(23, 50),
        time(0, 20),
    )
    assert is_within_window(
        datetime(2026, 9, 9, 0, 10, tzinfo=ZoneInfo("America/New_York")),
        time(23, 50),
        time(2, 5),
    )
    assert not is_within_window(
        datetime(2026, 9, 9, 2, 6, tzinfo=ZoneInfo("America/New_York")),
        time(23, 50),
        time(2, 5),
    )


def test_normal_failures_restart_at_threshold(tmp_path):
    config = configuration(tmp_path)
    state = WatchdogState(consecutive_failures=5)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)

    decision = restart_decision(config, state, now)

    assert decision.should_restart
    assert not decision.urgent


def test_maintenance_suppresses_restart_even_after_many_failures(tmp_path):
    config = configuration(tmp_path)
    state = WatchdogState(consecutive_failures=100)
    now = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # Midnight Eastern.

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert decision.status == "MAINTENANCE"
    assert "suppressed" in decision.reason


def test_weekend_is_quiet_until_sunday_morning():
    eastern = ZoneInfo("America/New_York")

    assert is_weekend_quiet(
        datetime(2026, 9, 12, 12, 0, tzinfo=eastern),
        time(8),
    )
    assert is_weekend_quiet(
        datetime(2026, 9, 13, 7, 59, tzinfo=eastern),
        time(8),
    )
    assert not is_weekend_quiet(
        datetime(2026, 9, 13, 8, 0, tzinfo=eastern),
        time(8),
    )


def test_one_restart_attempt_locks_out_the_rest_of_the_incident(tmp_path):
    config = configuration(tmp_path)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        consecutive_failures=8,
        automatic_restart_attempted=True,
    )

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert decision.urgent
    assert decision.status == "OPERATOR_REQUIRED"


def test_successful_restart_still_enforces_daily_cap(tmp_path):
    config = configuration(tmp_path)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        consecutive_failures=5,
        last_restart_at=(now.replace(hour=13)).isoformat(),
    )

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert decision.urgent
    assert "cap active" in decision.reason


def test_daily_restart_cap_expires(tmp_path):
    config = configuration(tmp_path)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        consecutive_failures=5,
        last_restart_at=datetime(2026, 9, 7, 13, 59, tzinfo=UTC).isoformat(),
    )

    decision = restart_decision(config, state, now)

    assert decision.should_restart


class HealthyIB:
    def __init__(self):
        self.connected = False
        self.positions_cancelled = 0
        self.client = SimpleNamespace(cancelPositions=self.cancelPositions)

    def isConnected(self):
        return self.connected

    async def connectAsync(self, *args, **kwargs):
        self.connected = True

    async def reqCurrentTimeAsync(self):
        return datetime(2026, 9, 10, 13, 0, tzinfo=UTC)

    async def reqContractDetailsAsync(self, contract):
        return [SimpleNamespace(contract=SimpleNamespace(conId=265598, secType="STK"))]

    def managedAccounts(self):
        return ["DU12345"]

    async def reqPositionsAsync(self):
        return [SimpleNamespace(position=1)]

    def cancelPositions(self):
        self.positions_cancelled += 1

    async def accountSummaryAsync(self, account):
        return [SimpleNamespace(tag="NetLiquidation", value="100000")]

    def disconnect(self):
        self.connected = False


@pytest.mark.asyncio
async def test_gateway_probe_requires_all_strategy_critical_apis(tmp_path):
    ib = HealthyIB()
    probe = GatewayProbe(configuration(tmp_path), ib=ib)

    result = await probe.check()

    assert result.healthy
    assert "AAPL conId 265598" in result.detail
    assert "1 positions" in result.detail
    assert ib.positions_cancelled == 1


@pytest.mark.asyncio
async def test_gateway_probe_rejects_partial_api_health(tmp_path):
    class PartiallyAliveIB(HealthyIB):
        async def reqContractDetailsAsync(self, contract):
            raise ConnectionError("contract-details channel is stuck")

    ib = PartiallyAliveIB()
    probe = GatewayProbe(configuration(tmp_path), ib=ib)

    result = await probe.check()

    assert not result.healthy
    assert "contract-details channel is stuck" in result.detail
    assert not ib.connected


class FixedProbe:
    def __init__(self, result: ProbeResult):
        self.result = result

    async def check(self) -> ProbeResult:
        return self.result

    def disconnect(self) -> None:
        pass


class FixedRestarter:
    def __init__(self, result: tuple[bool, str]):
        self.result = result
        self.calls = 0

    async def restart(self) -> tuple[bool, str]:
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_healthy_probe_clears_incident_lockout(tmp_path):
    restarter = FixedRestarter((True, "unused"))
    watchdog = Watchdog(
        configuration(tmp_path),
        FixedProbe(ProbeResult(True, "all APIs healthy")),
        restarter,
        clock=lambda: datetime(2026, 9, 8, 14, 0, tzinfo=UTC),
    )
    watchdog.state = WatchdogState(
        status="OPERATOR_REQUIRED",
        consecutive_failures=10,
        automatic_restart_attempted=True,
        operator_required=True,
    )

    assert await watchdog.run_cycle()
    assert watchdog.state.status == "HEALTHY"
    assert watchdog.state.consecutive_failures == 0
    assert not watchdog.state.automatic_restart_attempted
    assert not watchdog.state.operator_required
    assert restarter.calls == 0


@pytest.mark.asyncio
async def test_failed_restart_requires_an_operator(tmp_path):
    restarter = FixedRestarter((False, "Docker is unavailable"))
    watchdog = Watchdog(
        configuration(tmp_path, failure_threshold=1),
        FixedProbe(ProbeResult(False, "API unavailable")),
        restarter,
        clock=lambda: datetime(2026, 9, 8, 14, 0, tzinfo=UTC),
    )

    assert not await watchdog.run_cycle()
    assert watchdog.state.status == "OPERATOR_REQUIRED"
    assert watchdog.state.automatic_restart_attempted
    assert watchdog.state.operator_required
    assert restarter.calls == 1


@pytest.mark.asyncio
async def test_restart_command_has_a_timeout():
    restarter = GatewayRestarter(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        timeout_seconds=1,
    )

    restarted, detail = await restarter.restart()

    assert not restarted
    assert "timed out" in detail
