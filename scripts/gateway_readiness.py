"""Print and optionally email a Sunday IB Gateway readiness report."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime

try:
    from .gateway_watchdog import GatewayProbe, WatchdogConfig, WatchdogState
    from .watchdog_email import email_sender_from_environment
except ImportError:  # Support direct execution: python scripts/gateway_readiness.py
    from gateway_watchdog import GatewayProbe, WatchdogConfig, WatchdogState
    from watchdog_email import email_sender_from_environment


def service_is_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "ibkr-gateway-watchdog.service"],
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def build_report(
    *,
    now: datetime,
    probe_healthy: bool,
    probe_detail: str,
    service_active: bool,
    disk_percent: float,
    state: WatchdogState,
) -> tuple[bool, str]:
    ready = (
        probe_healthy
        and service_active
        and disk_percent < 90
        and not state.operator_required
    )
    label = "READY" if ready else "ACTION REQUIRED"
    body = "\n".join(
        (
            f"IBKR Gateway Sunday readiness: {label}",
            "",
            f"Generated: {now.isoformat()}",
            f"Gateway API probe: {'PASS' if probe_healthy else 'FAIL'}",
            f"Probe detail: {probe_detail}",
            f"Watchdog service: {'active' if service_active else 'inactive'}",
            f"Watchdog status: {state.status}",
            f"Last healthy probe: {state.last_healthy_at or 'none recorded'}",
            f"Last automatic restart: {state.last_restart_at or 'none recorded'}",
            f"Last restart outcome: {state.last_restart_outcome or 'none recorded'}",
            f"Disk usage: {disk_percent:.1f}%",
            f"Operator required: {'yes' if state.operator_required else 'no'}",
            "",
            "If action is required, inspect Gateway over the private VNC tunnel.",
        )
    )
    return ready, body


async def async_main(email: bool) -> int:
    config = WatchdogConfig.from_environment()
    readiness_client_id = int(os.getenv("IBKR_READINESS_CLIENT_ID", "996"))
    probe = GatewayProbe(replace(config, client_id=readiness_client_id))
    try:
        result = await probe.check()
    finally:
        probe.disconnect()

    disk = shutil.disk_usage("/")
    disk_percent = disk.used / disk.total * 100
    state = WatchdogState.load(config.state_path)
    ready, report = build_report(
        now=datetime.now(UTC),
        probe_healthy=result.healthy,
        probe_detail=result.detail,
        service_active=service_is_active(),
        disk_percent=disk_percent,
        state=state,
    )
    print(report)

    if email:
        try:
            sender = email_sender_from_environment()
        except ValueError as error:
            print(f"Readiness email is misconfigured: {error}")
            return 1
        if sender is None:
            print(
                "Email alerts are not configured; report written to the journal only."
            )
        else:
            label = "READY" if ready else "ACTION REQUIRED"
            sent, detail = await sender.send(
                f"[IBKR] Sunday readiness: {label}",
                report,
            )
            print(f"Readiness email: {detail}")
            if not sent:
                return 1

    return 0 if ready else 1


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        action="store_true",
        help="Email the report when optional SMTP settings are configured.",
    )
    return parser.parse_args()


def main() -> int:
    return asyncio.run(async_main(arguments().email))


if __name__ == "__main__":
    raise SystemExit(main())
