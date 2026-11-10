import asyncio
import time

import httpx
import pytest

from app.ahclient import AhApiError, AhClient, AhLoginExpired, AhNotJson, extract_code
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


async def test_redirect_with_empty_body_raises(respx_mock, tmp_path):
    respx_mock.get(BASE + "/x").respond(302, headers={"location": "https://login.ah.nl/"})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    with pytest.raises(AhApiError) as ei:
        await client.request("GET", "/x")
    assert ei.value.status == 302
    await http.aclose()


FAKE_CODE = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    "pasted",
    [
        f"Failed to launch 'appie://login-exit?code={FAKE_CODE}' because the scheme "
        "does not have a registered handler.",
        f"appie://login-exit?code={FAKE_CODE}",
        f"  {FAKE_CODE}\n",
    ],
    ids=["console-line", "bare-url", "bare-code"],
)
def test_extract_code(pasted):
    assert extract_code(pasted) == FAKE_CODE


# --- final review fixes ---------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_refused_refresh_is_login_expired(respx_mock, tmp_path, status):
    # a refused refresh is usually 400 invalid_grant: the user must log in again
    respx_mock.post(REFRESH_URL).respond(status, json={"error": "invalid_grant"})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 10))
    with pytest.raises(AhLoginExpired) as ei:
        await client.ensure_fresh()
    assert "invalid_grant" not in str(ei.value)
    await http.aclose()


async def test_refresh_500_stays_api_error(respx_mock, tmp_path):
    respx_mock.post(REFRESH_URL).respond(500, text="down")
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 10))
    with pytest.raises(AhApiError) as ei:
        await client.ensure_fresh()
    assert not isinstance(ei.value, AhLoginExpired) and ei.value.status == 500
    await http.aclose()


@pytest.mark.parametrize("status", [400, 403])
async def test_bad_login_code_is_not_login_expired(respx_mock, tmp_path, status):
    # login() must still report a bad code as a failed login, with its status
    respx_mock.post(TOKEN_URL).respond(status, json={})
    client, _, http = make(tmp_path)
    with pytest.raises(AhApiError) as ei:
        await client.exchange_code("abc")
    assert not isinstance(ei.value, AhLoginExpired) and ei.value.status == status
    await http.aclose()


class SlowRefreshHttp:
    """An http client whose refresh POST waits until `release` is set."""

    def __init__(self):
        self.posts = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, url, **kw):
        self.posts += 1
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json=token_body("fresh-a", "fresh-r"),
                              request=httpx.Request("POST", url))


async def test_cancelled_caller_still_saves_refreshed_token(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("old-a", "old-r", time.time() + 10))
    http = SlowRefreshHttp()
    client = AhClient(store, http)
    caller = asyncio.create_task(client.ensure_fresh())
    await http.started.wait()
    caller.cancel()  # e.g. asyncio.wait_for timing out mid-refresh
    with pytest.raises(asyncio.CancelledError):
        await caller
    http.release.set()
    await client._refresh_task  # the refresh itself was not cancelled
    t = store.load()
    assert (t.access_token, t.refresh_token) == ("fresh-a", "fresh-r")
    assert http.posts == 1


async def test_concurrent_ensure_fresh_makes_one_refresh(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("old-a", "old-r", time.time() + 10))
    http = SlowRefreshHttp()
    client = AhClient(store, http)
    both = asyncio.gather(client.ensure_fresh(), client.ensure_fresh())
    await http.started.wait()
    http.release.set()
    await both
    assert http.posts == 1  # the old refresh token is used once, never twice
    assert store.load().refresh_token == "fresh-r"


async def test_failed_refresh_reaches_every_waiter_and_next_call_tries_again(
        respx_mock, tmp_path):
    route = respx_mock.post(REFRESH_URL).mock(side_effect=[
        httpx.Response(500), httpx.Response(200, json=token_body())])
    client, store, http = make(tmp_path, Tokens("a", "r", time.time() + 10))
    with pytest.raises(AhApiError):
        await client.ensure_fresh()
    await client.ensure_fresh()  # a finished (failed) refresh is not reused
    assert route.call_count == 2 and store.load().refresh_token == "new-r"
    await http.aclose()


async def test_graphql_null_data_is_empty(respx_mock, tmp_path):
    respx_mock.post(BASE + "/graphql").respond(json={"data": None})
    client, _, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))
    assert await client.graphql("query { x }", {}) == {}
    await http.aclose()


async def test_request_raises_login_expired_when_tokens_vanish_after_ensure_fresh(
        respx_mock, tmp_path, monkeypatch):
    client, store, http = make(tmp_path, Tokens("a", "r", time.time() + 3600))

    async def fresh_then_gone():
        (tmp_path / "tokens.json").unlink()  # e.g. a logout between the two loads

    monkeypatch.setattr(client, "ensure_fresh", fresh_then_gone)
    with pytest.raises(AhLoginExpired):
        await client.request("GET", "/x")
    await http.aclose()
