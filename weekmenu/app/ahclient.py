import time
from typing import Any

import httpx

from .tokens import Tokens, TokenStore

BASE_URL = "https://api.ah.nl"
LOGIN_URL = (
    "https://login.ah.nl/login?client_id=appie-ios"
    "&response_type=code&redirect_uri=appie://login-exit"
)
# Bump together when AH refuses old app versions.
APP_VERSION = "9.28"
USER_AGENT = f"Appie/{APP_VERSION} (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"
REFRESH_MARGIN = 300


class AhApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AhLoginExpired(AhApiError):
    pass


class AhNotJson(AhApiError):
    pass


class AhClient:
    def __init__(self, store: TokenStore, http: httpx.AsyncClient):
        self.store = store
        self.http = http

    def _headers(self, order_id: int | None = None) -> dict[str, str]:
        h = {
            "User-Agent": USER_AGENT,
            "x-client-name": "appie-ios",
            "x-client-version": APP_VERSION,
            "x-application": "AHWEBSHOP",
            "Accept": "application/json",
        }
        if order_id is not None:
            h["Appie-Current-Order-Id"] = str(order_id)
        return h

    async def _token_call(self, path: str, body: dict) -> None:
        # Error messages never include response bodies: they may echo secrets.
        try:
            r = await self.http.post(
                BASE_URL + path,
                json=body,
                headers=self._headers(),
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise AhApiError(f"token request failed: {type(e).__name__}") from None
        if r.status_code == 401:
            raise AhLoginExpired("login expired", 401)
        if r.status_code != 200:
            raise AhApiError(f"token request failed: HTTP {r.status_code}", r.status_code)
        try:
            data = r.json()
            self.store.save(
                Tokens(
                    data["access_token"],
                    data["refresh_token"],
                    time.time() + float(data["expires_in"]),
                )
            )
        except (ValueError, KeyError, TypeError):
            raise AhNotJson("token response not valid", r.status_code) from None

    async def exchange_code(self, code: str) -> None:
        await self._token_call(
            "/mobile-auth/v1/auth/token", {"clientId": "appie-ios", "code": code}
        )

    async def refresh(self) -> None:
        t = self.store.load()
        if t is None:
            raise AhLoginExpired("not logged in", 401)
        await self._token_call(
            "/mobile-auth/v1/auth/token/refresh",
            {"clientId": "appie-ios", "refreshToken": t.refresh_token},
        )

    async def ensure_fresh(self) -> None:
        t = self.store.load()
        if t is None:
            raise AhLoginExpired("not logged in", 401)
        if t.expires_at - time.time() < REFRESH_MARGIN:
            await self.refresh()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        order_id: int | None = None,
    ) -> Any:
        await self.ensure_fresh()
        t = self.store.load()
        headers = self._headers(order_id)
        headers["Authorization"] = f"Bearer {t.access_token}"
        try:
            r = await self.http.request(
                method, BASE_URL + path, json=json, headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            raise AhApiError(f"{method} {path} failed: {type(e).__name__}") from None
        if r.status_code == 401:
            raise AhLoginExpired(f"{method} {path}: login expired", 401)
        if r.status_code >= 400 or 300 <= r.status_code < 400:
            raise AhApiError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise AhNotJson(f"{method} {path}: response is not JSON", r.status_code) from None

    async def graphql(self, query: str, variables: dict) -> dict:
        body = await self.request(
            "POST", "/graphql", json={"query": query, "variables": variables}
        )
        if isinstance(body, dict) and body.get("errors"):
            raise AhApiError("graphql returned errors", 200)
        return (body or {}).get("data", {})
