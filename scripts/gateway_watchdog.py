"""Monitor IB Gateway with a real API round trip and restart it if it stays stuck.

The watchdog keeps one lightweight, read-only IBKR API connection open. A TCP
listener or server-time response is not considered healthy: every probe must
also resolve a known contract and synchronize account positions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from ib_async import IB, StartupFetchNONE, Stock


LOGGER = logging.getLogger("ibkr_gateway_watchdog")
KNOWN_CONTRACT = Stock("AAPL", "SMART", "USD", primaryExchange="NASDAQ")


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def clock_time(name: str, default: str) -> time:
    try:
        return time.fromisoformat(os.getenv(name, default))
    except ValueError as error:
        raise ValueError(f"{name} must use HH:MM or HH:MM:SS") from error


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    host: str
    port: int
    client_id: int
    probe_interval_seconds: int
    probe_timeout_seconds: int
    failure_threshold: int
    maintenance_failure_threshold: int
    maintenance_start: time
    maintenance_end: time
    restart_cooldown_seconds: int
    restart_window_seconds: int
    maximum_restarts_per_window: int
    recovery_timeout_seconds: int
    recovery_probe_interval_seconds: int
    state_path: Path
    restart_command: tuple[str, ...]
    timezone: ZoneInfo

    @classmethod
    def from_environment(cls) -> WatchdogConfig:
        restart_command = tuple(
            shlex.split(
                os.getenv(
                    "IBKR_WATCHDOG_RESTART_COMMAND",
                    "/usr/bin/sudo -n /usr/local/sbin/restart-ibkr-gateway",
                )
            )
        )
        if not restart_command:
            raise ValueError("IBKR_WATCHDOG_RESTART_COMMAND cannot be empty")

        default_state = (
            Path.home()
            / ".local"
            / "state"
            / "ibkr-gateway-watchdog"
            / "state.json"
        )
        return cls(
            host=os.getenv("IBKR_WATCHDOG_HOST", "127.0.0.1"),
            port=positive_int("IBKR_WATCHDOG_PORT", 4002),
            client_id=positive_int("IBKR_WATCHDOG_CLIENT_ID", 997),
            probe_interval_seconds=positive_int(
                "IBKR_WATCHDOG_PROBE_INTERVAL_SECONDS", 60
            ),
            probe_timeout_seconds=positive_int(
                "IBKR_WATCHDOG_PROBE_TIMEOUT_SECONDS", 10
            ),
            failure_threshold=positive_int(
                "IBKR_WATCHDOG_FAILURE_THRESHOLD", 5
            ),
            maintenance_failure_threshold=positive_int(
                "IBKR_WATCHDOG_MAINTENANCE_FAILURE_THRESHOLD", 20
            ),
            maintenance_start=clock_time(
                "IBKR_WATCHDOG_MAINTENANCE_START", "23:50"
            ),
            maintenance_end=clock_time(
                "IBKR_WATCHDOG_MAINTENANCE_END", "00:20"
            ),
            restart_cooldown_seconds=positive_int(
                "IBKR_WATCHDOG_RESTART_COOLDOWN_SECONDS", 300
            ),
            restart_window_seconds=positive_int(
                "IBKR_WATCHDOG_RESTART_WINDOW_SECONDS", 1800
            ),
            maximum_restarts_per_window=positive_int(
                "IBKR_WATCHDOG_MAXIMUM_RESTARTS_PER_WINDOW", 2
            ),
            recovery_timeout_seconds=positive_int(
                "IBKR_WATCHDOG_RECOVERY_TIMEOUT_SECONDS", 180
            ),
            recovery_probe_interval_seconds=positive_int(
                "IBKR_WATCHDOG_RECOVERY_PROBE_INTERVAL_SECONDS", 10
            ),
            state_path=Path(
                os.getenv("IBKR_WATCHDOG_STATE_PATH", str(default_state))
            ).expanduser(),
            restart_command=restart_command,
            timezone=ZoneInfo(
                os.getenv("IBKR_WATCHDOG_TIME_ZONE", "America/New_York")
            ),
        )


@dataclass(slots=True)
class WatchdogState:
    consecutive_failures: int = 0
    restart_attempts: list[str] = field(default_factory=list)
    last_check_at: str | None = None
    last_healthy_at: str | None = None
    last_error: str | None = None
    last_restart_at: str | None = None
    last_restart_outcome: str | None = None

    @classmethod
    def load(cls, path: Path) -> WatchdogState:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError, TypeError) as error:
            LOGGER.warning("Could not read watchdog state %s: %s", path, error)
            return cls()

        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in document.items() if key in allowed})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    healthy: bool
    detail: str


class Probe(Protocol):
    async def check(self) -> ProbeResult: ...

    def disconnect(self) -> None: ...


class GatewayProbe:
    """Maintain one read-only connection and verify strategy-critical APIs."""

    def __init__(self, config: WatchdogConfig, ib: IB | None = None):
        self.config = config
        self.ib = ib or IB()

    async def check(self) -> ProbeResult:
        try:
            if not self.ib.isConnected():
                await asyncio.wait_for(
                    self.ib.connectAsync(
                        self.config.host,
                        self.config.port,
                        clientId=self.config.client_id,
                        readonly=True,
                        raiseSyncErrors=False,
                        fetchFields=StartupFetchNONE,
                    ),
                    timeout=self.config.probe_timeout_seconds,
                )
            server_time = await asyncio.wait_for(
                self.ib.reqCurrentTimeAsync(),
                timeout=self.config.probe_timeout_seconds,
            )
            if server_time is None:
                raise RuntimeError("IBKR returned no server time")

            details = await asyncio.wait_for(
                self.ib.reqContractDetailsAsync(KNOWN_CONTRACT),
                timeout=self.config.probe_timeout_seconds,
            )
            contract = next(
                (
                    item.contract
                    for item in details
                    if item.contract.conId > 0 and item.contract.secType == "STK"
                ),
                None,
            )
            if contract is None:
                raise RuntimeError("IBKR could not resolve the known AAPL contract")

            accounts = self.ib.managedAccounts()
            if not accounts:
                raise RuntimeError("IBKR returned no managed accounts")
            try:
                positions = await asyncio.wait_for(
                    self.ib.reqPositionsAsync(),
                    timeout=self.config.probe_timeout_seconds,
                )
            finally:
                if self.ib.isConnected():
                    self.ib.client.cancelPositions()

            account_values = await asyncio.wait_for(
                self.ib.accountSummaryAsync(accounts[0]),
                timeout=self.config.probe_timeout_seconds,
            )
            if not any(value.tag == "NetLiquidation" for value in account_values):
                raise RuntimeError("IBKR account summary omitted NetLiquidation")

            return ProbeResult(
                True,
                (
                    f"server time {server_time.isoformat()}, "
                    f"AAPL conId {contract.conId}, "
                    f"{len(positions)} positions, "
                    f"{len(accounts)} managed account(s)"
                ),
            )
        except Exception as error:
            self.disconnect()
            return ProbeResult(False, f"{type(error).__name__}: {error}")

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()


class GatewayRestarter:
    def __init__(self, command: tuple[str, ...]):
        self.command = command

    async def restart(self) -> tuple[bool, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
        except Exception as error:
            return False, f"could not execute restart command: {error}"

        detail = output.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            return False, detail or f"restart exited {process.returncode}"
        return True, detail or "restart command completed"


def is_within_window(moment: datetime, start: time, end: time) -> bool:
    current = moment.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def parsed_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def recent_restart_attempts(
    state: WatchdogState,
    now: datetime,
    window_seconds: int,
) -> list[str]:
    cutoff = now.astimezone(UTC) - timedelta(seconds=window_seconds)
    recent = []
    for value in state.restart_attempts:
        parsed = parsed_timestamp(value)
        if parsed is not None and parsed >= cutoff:
            recent.append(parsed.isoformat())
    state.restart_attempts = recent
    return recent


@dataclass(frozen=True, slots=True)
class RestartDecision:
    should_restart: bool
    reason: str
    urgent: bool = False


def restart_decision(
    config: WatchdogConfig,
    state: WatchdogState,
    now: datetime,
) -> RestartDecision:
    local_now = now.astimezone(config.timezone)
    maintenance = is_within_window(
        local_now,
        config.maintenance_start,
        config.maintenance_end,
    )
    threshold = (
        config.maintenance_failure_threshold
        if maintenance
        else config.failure_threshold
    )
    if state.consecutive_failures < threshold:
        label = "maintenance" if maintenance else "normal"
        return RestartDecision(
            False,
            f"failure {state.consecutive_failures}/{threshold} ({label} threshold)",
        )

    attempts = recent_restart_attempts(
        state,
        now,
        config.restart_window_seconds,
    )
    if len(attempts) >= config.maximum_restarts_per_window:
        return RestartDecision(
            False,
            "automatic restart limit reached; manual attention required",
            urgent=True,
        )

    if attempts:
        last_attempt = parsed_timestamp(attempts[-1])
        assert last_attempt is not None
        age = (now.astimezone(UTC) - last_attempt).total_seconds()
        if age < config.restart_cooldown_seconds:
            remaining = config.restart_cooldown_seconds - age
            return RestartDecision(
                False,
                f"restart cooldown active for another {remaining:.0f} seconds",
            )

    return RestartDecision(True, f"Gateway failed {state.consecutive_failures} probes")


class Watchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        probe: Probe,
        restarter: GatewayRestarter,
    ):
        self.config = config
        self.probe = probe
        self.restarter = restarter
        self.state = WatchdogState.load(config.state_path)

    def save(self) -> None:
        self.state.save(self.config.state_path)

    async def wait_for_recovery(self) -> ProbeResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.recovery_timeout_seconds
        latest = ProbeResult(False, "recovery has not been checked")
        while loop.time() < deadline:
            await asyncio.sleep(self.config.recovery_probe_interval_seconds)
            latest = await self.probe.check()
            if latest.healthy:
                return latest
        return latest

    async def run_cycle(self) -> bool:
        checked_at = datetime.now(UTC)
        result = await self.probe.check()
        self.state.last_check_at = checked_at.isoformat()

        if result.healthy:
            if self.state.consecutive_failures:
                LOGGER.info(
                    "Gateway recovered after %d failed probes: %s",
                    self.state.consecutive_failures,
                    result.detail,
                )
            else:
                LOGGER.debug("Gateway healthy: %s", result.detail)
            self.state.consecutive_failures = 0
            self.state.last_healthy_at = checked_at.isoformat()
            self.state.last_error = None
            self.save()
            return True

        self.state.consecutive_failures += 1
        self.state.last_error = result.detail
        decision = restart_decision(self.config, self.state, checked_at)
        log = LOGGER.critical if decision.urgent else LOGGER.warning
        log("Gateway probe failed: %s; %s", result.detail, decision.reason)
        self.save()
        if not decision.should_restart:
            return False

        self.state.restart_attempts.append(checked_at.isoformat())
        self.state.last_restart_at = checked_at.isoformat()
        self.state.last_restart_outcome = "STARTED"
        self.save()
        LOGGER.error("Restarting the IB Gateway container")
        restarted, restart_detail = await self.restarter.restart()
        if not restarted:
            self.state.last_restart_outcome = "COMMAND_FAILED"
            self.state.last_error = restart_detail
            self.save()
            LOGGER.critical("Gateway restart command failed: %s", restart_detail)
            return False

        LOGGER.info("Gateway restart command completed: %s", restart_detail)
        self.probe.disconnect()
        recovery = await self.wait_for_recovery()
        if not recovery.healthy:
            self.state.last_restart_outcome = "STILL_UNHEALTHY"
            self.state.last_error = recovery.detail
            self.save()
            LOGGER.critical(
                "Gateway did not recover within %d seconds: %s",
                self.config.recovery_timeout_seconds,
                recovery.detail,
            )
            return False

        recovered_at = datetime.now(UTC)
        self.state.consecutive_failures = 0
        self.state.last_check_at = recovered_at.isoformat()
        self.state.last_healthy_at = recovered_at.isoformat()
        self.state.last_error = None
        self.state.last_restart_outcome = "RECOVERED"
        self.save()
        LOGGER.info("Gateway recovered after container restart: %s", recovery.detail)
        return True

    async def run_forever(self) -> None:
        LOGGER.info(
            "Watching IB Gateway at %s:%d every %d seconds",
            self.config.host,
            self.config.port,
            self.config.probe_interval_seconds,
        )
        try:
            while True:
                await self.run_cycle()
                await asyncio.sleep(self.config.probe_interval_seconds)
        finally:
            self.probe.disconnect()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Run one API round-trip without changing state or restarting Gateway.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one managed watchdog cycle instead of staying resident.",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    config = WatchdogConfig.from_environment()
    probe = GatewayProbe(config)
    try:
        if args.probe_only:
            result = await probe.check()
            log = LOGGER.info if result.healthy else LOGGER.error
            log(
                "Gateway probe %s: %s",
                "passed" if result.healthy else "failed",
                result.detail,
            )
            return 0 if result.healthy else 1

        watchdog = Watchdog(
            config,
            probe,
            GatewayRestarter(config.restart_command),
        )
        if args.once:
            return 0 if await watchdog.run_cycle() else 1
        await watchdog.run_forever()
        return 0
    finally:
        probe.disconnect()


def main() -> int:
    logging.basicConfig(
        level=os.getenv("IBKR_WATCHDOG_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("ib_async").setLevel(
        os.getenv("IBKR_WATCHDOG_IB_ASYNC_LOG_LEVEL", "WARNING").upper()
    )
    return asyncio.run(async_main(arguments()))


if __name__ == "__main__":
    raise SystemExit(main())
