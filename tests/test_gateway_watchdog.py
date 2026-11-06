from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts.gateway_watchdog import (
    GatewayProbe,
    WatchdogConfig,
    WatchdogState,
    is_within_window,
    recent_restart_attempts,
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
        "maintenance_failure_threshold": 20,
        "maintenance_start": time(23, 50),
        "maintenance_end": time(0, 20),
        "restart_cooldown_seconds": 300,
        "restart_window_seconds": 1800,
        "maximum_restarts_per_window": 2,
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
        time(0, 20),
    )
    assert not is_within_window(
        datetime(2026, 9, 9, 0, 21, tzinfo=ZoneInfo("America/New_York")),
        time(23, 50),
        time(0, 20),
    )


def test_normal_failures_restart_at_threshold(tmp_path):
    config = configuration(tmp_path)
    state = WatchdogState(consecutive_failures=5)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)

    decision = restart_decision(config, state, now)

    assert decision.should_restart
    assert not decision.urgent


def test_maintenance_uses_longer_failure_threshold(tmp_path):
    config = configuration(tmp_path)
    state = WatchdogState(consecutive_failures=5)
    now = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)  # Midnight Eastern.

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert "5/20" in decision.reason


def test_restart_cooldown_prevents_an_immediate_second_attempt(tmp_path):
    config = configuration(tmp_path)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        consecutive_failures=8,
        restart_attempts=[(now - timedelta(seconds=60)).isoformat()],
    )

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert "cooldown" in decision.reason


def test_restart_limit_requires_manual_attention(tmp_path):
    config = configuration(tmp_path)
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        consecutive_failures=10,
        restart_attempts=[
            (now - timedelta(minutes=20)).isoformat(),
            (now - timedelta(minutes=10)).isoformat(),
        ],
    )

    decision = restart_decision(config, state, now)

    assert not decision.should_restart
    assert decision.urgent


def test_old_restart_attempts_are_pruned():
    now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    state = WatchdogState(
        restart_attempts=[
            (now - timedelta(hours=1)).isoformat(),
            (now - timedelta(minutes=5)).isoformat(),
        ]
    )

    attempts = recent_restart_attempts(state, now, window_seconds=1800)

    assert attempts == [(now - timedelta(minutes=5)).isoformat()]


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
        return [
            SimpleNamespace(
                contract=SimpleNamespace(conId=265598, secType="STK")
            )
        ]

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
