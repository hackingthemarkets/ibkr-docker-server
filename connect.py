"""Complete one read-only API handshake with TWS or IB Gateway.

Usage:
    python connect.py                 # local paper TWS (port 7497)
    IBKR_PORT=4002 python connect.py  # Gateway, run on the server
    IBKR_PORT=14002 python connect.py # server Gateway through a tunnel
"""

import asyncio
import os

from ib_async import IB, StartupFetchNONE

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "7497"))
CLIENT_ID = 101


async def main() -> None:
    ib = IB()
    try:
        await ib.connectAsync(
            HOST,
            PORT,
            clientId=CLIENT_ID,
            readonly=True,
            raiseSyncErrors=False,
            fetchFields=StartupFetchNONE,
        )
        print("Connected:", ib.isConnected())
        print("Managed accounts:", len(ib.managedAccounts()))
        print("Server version:", ib.client.serverVersion())
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
