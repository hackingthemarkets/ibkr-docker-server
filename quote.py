"""Check that your paper login receives real-time US stock quotes.

Usage:
    python quote.py                 # AAPL from Gateway, run on the server (port 4002)
    IBKR_PORT=14002 python quote.py # server Gateway through a tunnel
    IBKR_PORT=7497 python quote.py  # local paper TWS
    QUOTE_SYMBOL=MSFT python quote.py # another US stock
"""

import asyncio
import math
import os

from ib_async import IB, StartupFetchNONE, Stock

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "4002"))
CLIENT_ID = 102
SYMBOL = os.getenv("QUOTE_SYMBOL", "AAPL")
WAIT_SECONDS = 10

# What IBKR reports in ticker.marketDataType.
DATA_TYPES = {
    1: "real-time",
    2: "frozen (real-time subscription, market closed: last available quote)",
    3: "delayed",
    4: "delayed frozen",
}

# Errors that mean the login has no usable subscription for this stock.
NOT_SUBSCRIBED = {354, 10089, 10090, 10167, 10168}


def value(number) -> str:
    return "-" if number is None or math.isnan(number) else f"{number:.2f}"


def has_price(ticker) -> bool:
    return any(
        number is not None and not math.isnan(number)
        for number in (ticker.bid, ticker.ask, ticker.last)
    )


async def main() -> None:
    ib = IB()
    errors: list[tuple[int, str]] = []
    ib.errorEvent += lambda req_id, code, message, contract: errors.append((code, message))
    try:
        await ib.connectAsync(
            HOST,
            PORT,
            clientId=CLIENT_ID,
            readonly=True,
            raiseSyncErrors=False,
            fetchFields=StartupFetchNONE,
        )
        contract = Stock(SYMBOL, "SMART", "USD")
        await ib.qualifyContractsAsync(contract)

        # 2 = real-time while the market is open, the last real-time quote while
        # it's closed. Either way it requires a real-time subscription.
        ib.reqMarketDataType(2)
        ticker = ib.reqMktData(contract, genericTickList="")
        for _ in range(WAIT_SECONDS * 4):
            if has_price(ticker):
                break
            if any(code in NOT_SUBSCRIBED for code, _ in errors):
                break
            await asyncio.sleep(0.25)
        ib.cancelMktData(contract)

        print("Symbol:", SYMBOL)
        # marketDataType defaults to 1, so it only means something once a price arrives.
        received = has_price(ticker)
        data_type = DATA_TYPES.get(ticker.marketDataType, "unknown") if received else "none received"
        print("Data type:", data_type)
        print("Bid / Ask / Last:", value(ticker.bid), "/", value(ticker.ask), "/", value(ticker.last))
        for code, message in errors:
            print(f"IBKR message {code}: {message}")

        if received and ticker.marketDataType in (1, 2):
            print("Result: real-time market data is working for this login.")
        else:
            print(
                "Result: no real-time quote. Check your market data subscription "
                "and that it's shared with the paper account."
            )
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
