"""Login go/no-go test: python -m tools.logintest [--rounds N] [--interval S]"""
import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from app.ahclient import LOGIN_URL, AhClient
from app.tokens import TokenStore


def extract_code(line: str) -> str:
    line = line.strip()
    if "code=" in line:
        qs = parse_qs(urlparse(line).query)
        if "code" in qs:
            return qs["code"][0]
        return line.split("code=", 1)[1].split("&", 1)[0]
    return line


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
        await client.exchange_code(code)
        print("ingelogd")
        for i in range(rounds):
            if i:
                await asyncio.sleep(interval)
            await client.refresh()
            data = await client.request("GET", "/mobile-services/shoppinglist/v2/items")
            items = data.get("items", []) if isinstance(data, dict) else data
            print(f"{datetime.now().isoformat(timespec='seconds')} refresh=ja items={len(items)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--interval", type=float, default=60)
    a = p.parse_args()
    asyncio.run(main(a.rounds, a.interval))
