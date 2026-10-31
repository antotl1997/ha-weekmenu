import time

import httpx
import pytest

from app.ahclient import AhApiError, AhClient, AhLoginExpired, AhNotJson
from app.tokens import Tokens, TokenStore

BASE = "https://api.ah.nl"
TOKEN_URL = BASE + "/mobile-auth/v1/auth/token"
REFRESH_URL = BASE + "/mobile-auth/v1/auth/token/refresh"
SECRET_ACCESS = "secret-access-token-xyz"


def make(tmp_path, tokens=None):
    store = TokenStore(tmp_path / "tokens.json")
    if tokens:
        store.save(tokens)
    http = httpx.AsyncClient()
    return AhClient(store, http), store, http


def token_body(access="new-a", refresh="new-r", expires_in=7200):
    return {"access_token": access, "refresh_token": refresh, "expires_in": expires_in}


async def test_exchange_code_stores_tokens(respx_mock, tmp_path):
    route = respx_mock.post(TOKEN_URL).respond(json=token_body())
    client, store, http = make(tmp_path)
    await client.exchange_code("abc")
    t = store.load()
    assert (t.access_token, t.refresh_token) == ("new-a", "new-r")
    assert t.expires_at > time.time() + 7000
    assert b'"clientId"' in route.calls[0].request.content
    await http.aclose()


async def test_ensure_fresh_refreshes_when_under_5_min(respx_mock, tmp_path):
    route = respx_mock.post(REFRESH_URL).respond(json=token_body())
    client, store, http = make(tmp_path, Tokens("old-a", "old-r", time.time() + 60))
    await client.ensure_fresh()
    assert route.call_count == 1
    assert store.load().refresh_token == "new-r"
    await http.aclose()


async def test_ensure_fresh_skips_when_valid(respx_mock, tmp_path):
    route = respx_mock.post(REFRESH_URL).respond(json=token_body())
    client, store, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    await client.ensure_fresh()
    assert route.call_count == 0
    await http.aclose()


async def test_refresh_401_raises_login_expired(respx_mock, tmp_path):
    respx_mock.post(REFRESH_URL).respond(401, json={})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 10))
    with pytest.raises(AhLoginExpired):
        await client.ensure_fresh()
    await http.aclose()


async def test_html_body_raises_not_json(respx_mock, tmp_path):
    respx_mock.get(BASE + "/x").respond(200, text="<html>blocked</html>")
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    with pytest.raises(AhNotJson):
        await client.request("GET", "/x")
    await http.aclose()


async def test_5xx_raises_api_error_with_status(respx_mock, tmp_path):
    respx_mock.get(BASE + "/x").respond(503, text="down")
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    with pytest.raises(AhApiError) as ei:
        await client.request("GET", "/x")
    assert ei.value.status == 503
    await http.aclose()


async def test_request_401_raises_login_expired(respx_mock, tmp_path):
    respx_mock.get(BASE + "/x").respond(401, json={})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    with pytest.raises(AhLoginExpired):
        await client.request("GET", "/x")
    await http.aclose()


async def test_headers_and_order_id(respx_mock, tmp_path):
    route = respx_mock.get(BASE + "/x").respond(json={"ok": 1})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    assert await client.request("GET", "/x", order_id=42) == {"ok": 1}
    h = route.calls[0].request.headers
    assert h["x-client-name"] == "appie-ios"
    assert h["x-application"] == "AHWEBSHOP"
    assert h["appie-current-order-id"] == "42"
    assert h["authorization"] == "Bearer a"
    await http.aclose()


async def test_graphql_errors_raise(respx_mock, tmp_path):
    respx_mock.post(BASE + "/graphql").respond(json={"errors": [{"message": "bad"}]})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    with pytest.raises(AhApiError):
        await client.graphql("query { x }", {})
    await http.aclose()


async def test_graphql_returns_data(respx_mock, tmp_path):
    respx_mock.post(BASE + "/graphql").respond(json={"data": {"x": 1}})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    assert await client.graphql("query { x }", {"v": 1}) == {"x": 1}
    await http.aclose()


async def test_token_never_in_exception_text(respx_mock, tmp_path):
    respx_mock.get(BASE + "/x").respond(500, text=f"echo {SECRET_ACCESS}")
    client, _, http = make(tmp_path, Tokens(SECRET_ACCESS, "r", time.time() + 3600))
    with pytest.raises(AhApiError) as ei:
        await client.request("GET", "/x")
    assert SECRET_ACCESS not in str(ei.value)
    await http.aclose()
