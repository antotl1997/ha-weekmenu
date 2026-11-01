"""Login go/no-go test: python -m tools.logintest [--rounds N] [--interval S]"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

from app.ahclient import LOGIN_URL, AhApiError, AhClient, AhLoginExpired, extract_code
from app.tokens import TokenStore


def read_line() -> str:
    if sys.stdin.isatty():
        return input("Plak de appie://login-exit?code=... regel: ")
    return sys.stdin.readline()


async def main(rounds: int, interval: float) -> None:
    path = Path(os.environ["APPDATA"]) / "weekmenu-test" / "tokens.json"
    print(LOGIN_URL)
    code = extract_code(read_line())
    async with httpx.AsyncClient(timeout=30) as http:
        client = AhClient(TokenStore(path), http)
        try:
            await client.exchange_code(code)
            print("ingelogd")
        except AhApiError as e:
            print(f"inloggen mislukt: {e}")
            return
        for i in range(rounds):
            if i:
                await asyncio.sleep(interval)
            try:
                await client.refresh()
                data = await client.request(
                    "GET", "/mobile-services/shoppinglist/v2/items"
                )
            except AhLoginExpired:
                print("login geweigerd")
                return
            stamp = datetime.now().isoformat(timespec="seconds")
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                print(f"{stamp} refresh=ja items={len(data['items'])}")
            else:
                print(f"{stamp} refresh=ja onverwacht antwoord, geen items-lijst")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--interval", type=float, default=60)
    a = p.parse_args()
    asyncio.run(main(a.rounds, a.interval))
