import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import httpx

from .health import ApiHealth
from .tokens import Tokens, TokenStore

if TYPE_CHECKING:
    from .staples import Purchase

BASE_URL = "https://api.ah.nl"
LOGIN_URL = (
    "https://login.ah.nl/login?client_id=appie-ios"
    "&response_type=code&redirect_uri=appie://login-exit"
)
# The default of the `ah_app_version` option: raise it there when AH refuses old versions.
APP_VERSION = "9.28"


def user_agent(version: str) -> str:
    return f"Appie/{version} (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"


USER_AGENT = user_agent(APP_VERSION)
REFRESH_MARGIN = 300
log = logging.getLogger("weekmenu.ahclient")


def extract_code(text: str) -> str:
    """Pull the login code out of a pasted URL, console line or bare code."""
    text = text.strip()
    if "code=" in text:
        text = text.split("code=", 1)[1]
    m = re.match(r"[A-Za-z0-9-]+", text)
    return m.group(0) if m else ""


class AhApiError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AhLoginExpired(AhApiError):
    pass


class AhNotJson(AhApiError):
    pass


class AhOrderMode(AhApiError):
    """The shopping list is locked because a delivery order is active."""


class AhShapeError(AhApiError):
    """AH answered, but a field the add-on reads is missing or of the wrong type."""


_SHAPE = (KeyError, TypeError, ValueError, AttributeError)


def health_kind(endpoint: str, e: BaseException) -> str | None:
    """How a failure counts for ApiHealth ("vorm", "HTTP 403"), or None when it does not:
    an expired login, the list locked by an order, a 404 on the order (no active order),
    a 5xx or a network error are not a change of the API."""
    if isinstance(e, (AhLoginExpired, AhOrderMode)):
        return None
    if isinstance(e, AhShapeError):
        return "vorm"
    if isinstance(e, AhNotJson):
        return "geen JSON"
    if isinstance(e, AhApiError) and 400 <= e.status < 500:
        if e.status == 404 and endpoint.startswith("bestelling"):
            return None
        return f"HTTP {e.status}"
    return None


def is_blocking(e: BaseException) -> bool:
    """Errors that mean AH is refusing us (also a WAF block, 403): stop the round at once."""
    return isinstance(e, (AhLoginExpired, AhNotJson)) or (
        isinstance(e, AhApiError) and e.status in (403, 429)
    )


def _list_of(body: Any, key: str, what: str) -> list:
    """body[key] as a list; a missing key or another type is a shape error."""
    if not isinstance(body, dict) or not isinstance(body.get(key), list):
        raise AhShapeError(f"{what}: veld {key} ontbreekt of is geen lijst", 200)
    return body[key]


def _int(v: Any, what: str) -> int:
    """An int (or a whole number string); anything else is a shape error."""
    if isinstance(v, bool):
        raise AhShapeError(f"{what}: geen getal", 200)
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    raise AhShapeError(f"{what}: geen getal", 200)


@dataclass
class RawResponse:
    status: int
    body: Any  # parsed JSON; raw text when not JSON; None when empty
    is_json: bool
    text: str


@dataclass
class BonusPeriod:
    start: date
    end: date
    next_visible_from: date | None
    categories: list[str]


@dataclass
class Product:
    id: int
    title: str
    unit_size: str | None
    regular: float
    now: float
    is_bonus: bool
    mechanism: str | None
    orderable: bool
    category: str | None
    hq_id: int | None = None  # AH article number; the only product id on a till receipt
    image: str | None = None  # photo for the Aanbiedingen tab (a group deal: its combined picture)
    # the AH bonus group (one promotion, e.g. "2 voor 2.49" on several small packs) this
    # product came from, and AH's own description of it; None for a single-product deal
    group_id: str | None = None
    group_title: str | None = None


@dataclass
class RecipeSummary:
    id: int
    title: str
    slug: str
    courses: list[str]
    cook: int | None
    oven: int | None
    wait: int | None
    servings: int
    image: str | None = None


@dataclass
class Recipe:
    id: int
    title: str
    url: str
    cook: int | None
    oven: int | None
    wait: int | None
    courses: list[str]
    cuisines: list[str]
    servings: int
    ingredients: list[tuple[str, str]]
    steps: list[str]
    image: str | None


@dataclass
class DeliveredOrder:
    """v1.0: a delivered order (read only): webshop id -> quantity really delivered.

    `state` is AH's orderState. Anything but "DELIVERED" (and not cancelled) means the
    order is not delivered yet: no items, and `day` may be unknown."""
    id: int
    day: date | None
    items: dict[int, int] = field(default_factory=dict)
    state: str = "DELIVERED"

    @property
    def delivered(self) -> bool:
        return self.state == "DELIVERED"


@dataclass
class ActiveOrder:
    id: int
    items: dict[int, int] = field(default_factory=dict)
    # the delivery day, when the summary itself names it (else see order_delivery_date)
    delivery_date: date | None = None


GROUP_QUERY = (
    "query BonusGroupProducts($id: String, $start: String, $end: String) {"
    " bonusPromotions(input: {id: $id, periodStart: $start, periodEnd: $end}) {"
    " products { id title brand salesUnitSize priceV2 { now { amount }"
    " was { amount } discount { description } } } } }"
)
RECIPE_SEARCH_QUERY = (
    "query RecipeSearch($query: RecipeSearchParams!) {"
    " recipeSearch(query: $query) { result { id title slug courses"
    " time { cook oven wait } serving { number type }"
    " images { url width height } } } }"
)
RECIPE_QUERY = (
    "query Recipe($id: Int!, $servings: Int) {"
    " recipe(id: $id, servings: $servings) { id title href cookTime courses"
    " cuisines images { url width height } servings { number type }"
    " ingredients { text name { singular } } preparation { steps } } }"
)
# Purchase history (albert-heijn-mcp src/ahapi/orders.ts and receipts.ts). Read-only.
# Receipt product lines carry `id`: the hq id (AH article number), not the webshop id.
FULFILLMENTS_QUERY = (
    "query Fulfillments { orderFulfillments(status: CLOSED) { result {"
    " orderId statusDescription shoppingType"
    " delivery { slot { date } } } } }"
)
# Open orders with their delivery day (albert-heijn-mcp getFulfillments, status OPEN).
OPEN_FULFILLMENTS_QUERY = (
    "query Fulfillments { orderFulfillments(status: OPEN) { result {"
    " orderId delivery { slot { date } } } } }"
)
RECEIPTS_QUERY = (
    "query Receipts($limit: Int!) {"
    " posReceiptsPage(pagination: {offset: 0, limit: $limit}) {"
    " posReceipts { id dateTime } } }"
)
RECEIPT_QUERY = (
    "query Receipt($id: String!) { posReceiptDetails(id: $id) {"
    " id products { id name quantity } } }"
)
RECEIPTS_LIMIT = 50
DELIVERED = "DELIVERED"
ORDER_MODE_TEXT = "Server in order mode"
# Pause between AH calls while walking the bonus folder (tests set this to 0).
CALL_DELAY = 0.2
MAX_CONSECUTIVE_CATEGORY_FAILURES = 3


def _num(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def _widest_image(images: Any) -> str | None:
    best, best_w = None, -1
    for im in images or []:
        if isinstance(im, dict) and im.get("url") and (im.get("width") or 0) > best_w:
            best, best_w = im["url"], im.get("width") or 0
    return best


def product_from_card(c: dict) -> Product:
    """REST product card (bonus section, product search)."""
    regular = c.get("priceBeforeBonus")
    now = c.get("currentPrice")
    if now is None:
        now = regular
    if regular is None:
        regular = now
    return Product(
        id=int(c["webshopId"]),
        title=html.unescape(c.get("title") or ""),
        unit_size=c.get("salesUnitSize"),
        regular=_num(regular),
        now=_num(now),
        is_bonus=bool(c.get("isBonus")),
        mechanism=c.get("bonusMechanism") or None,
        orderable=bool(c.get("isOrderable", True)),
        category=c.get("mainCategory") or None,
        hq_id=_pos_int(c.get("hqId")),
        image=_widest_image(c.get("images")),
    )


def product_from_graphql(
    p: dict, is_bonus: bool = True, category: str | None = None,
    image: str | None = None,
) -> Product:
    """GraphQL product (bonus group expansion). GraphQL has no category and no photo:
    the caller passes the group's own (the planner filters on the category, the
    Aanbiedingen tab shows the group's combined picture)."""
    price = p.get("priceV2") or {}
    now = (price.get("now") or {}).get("amount")
    was = (price.get("was") or {}).get("amount")
    if now is None:
        now = was
    if was is None:
        was = now
    return Product(
        id=int(p["id"]),
        title=html.unescape(p.get("title") or ""),
        unit_size=p.get("salesUnitSize"),
        regular=_num(was),
        now=_num(now),
        is_bonus=is_bonus,
        mechanism=(price.get("discount") or {}).get("description") or None,
        orderable=True,
        category=category,
        image=image,
    )


def _pos_int(v: Any) -> int | None:
    """A real id, or None (AH sends 0 for "unknown")."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _iso(d: Any) -> date | None:
    return date.fromisoformat(d) if d else None


def _local_day(stamp: Any) -> date | None:
    """The Dutch calendar day of an ISO time stamp ("2026-09-28T11:58:00.000Z")."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.date()
    try:
        zone = ZoneInfo("Europe/Amsterdam")
    except Exception:  # no tz database (Windows without tzdata): winter time is close enough
        zone = timezone(timedelta(hours=1))
    return dt.astimezone(zone).date()


def _minutes(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _list_item(pid: int, quantity: int) -> dict:
    """One shopping list PATCH item; the PATCH sets this quantity (0 removes it)."""
    return {
        "description": "",
        "productId": pid,
        "quantity": quantity,
        "type": "SHOPPABLE",
        "originCode": "PRD",
        "strikeThrough": False,
    }


def _order_item(pid: int, quantity: int) -> dict:
    """One order PUT item; the PUT sets this quantity (0 removes it)."""
    return {
        "productId": pid,
        "quantity": quantity,
        "originCode": "PRD",
        "description": "",
        "strikethrough": False,
    }


def _check_reductions(reductions: dict[int, int]) -> None:
    for r in reductions.values():
        if r <= 0:
            raise ValueError("reductions must be positive")


class AhClient:
    def __init__(self, store: TokenStore, http: httpx.AsyncClient, *,
                 health: ApiHealth | None = None, app_version: str = APP_VERSION):
        self.store = store
        self.http = http
        self.health = health if health is not None else ApiHealth()
        self.app_version = app_version or APP_VERSION
        self.skipped_categories: list[str] = []
        self.skipped_groups: list[str] = []
        self._called_ah = False
        self._refresh_task: asyncio.Task | None = None

    def _headers(self, order_id: int | None = None) -> dict[str, str]:
        h = {
            "User-Agent": user_agent(self.app_version),
            "x-client-name": "appie-ios",
            "x-client-version": self.app_version,
            "x-application": "AHWEBSHOP",
            "Accept": "application/json",
        }
        if order_id is not None:
            h["Appie-Current-Order-Id"] = str(order_id)
        return h

    async def _track(self, endpoint: str, awaitable):
        """Await a read or write of a critical endpoint and tell ApiHealth how it went.

        A KeyError, TypeError, ValueError or AttributeError while parsing becomes an
        AhShapeError (the type name only, never a body). A 403 is logged: AH refuses us.
        """
        try:
            out = await awaitable
        except _SHAPE as e:
            err = AhShapeError(f"{endpoint}: onverwachte vorm ({type(e).__name__})", 200)
            self._failed(endpoint, err)
            raise err from None
        except AhApiError as e:
            self._failed(endpoint, e)
            raise
        self.health.ok(endpoint)
        return out

    @staticmethod
    async def _untracked(endpoint: str, awaitable):
        """Like `_track`, but ApiHealth never hears of it (Ruling 53: the recount after
        the delivery may not pause writing). A parse error still becomes AhShapeError."""
        try:
            return await awaitable
        except _SHAPE as e:
            raise AhShapeError(f"{endpoint}: onverwachte vorm ({type(e).__name__})",
                               200) from None

    def _failed(self, endpoint: str, e: AhApiError) -> None:
        if e.status == 403:
            log.warning("AH weigert de toegang (HTTP 403) bij %s: ronde gestopt", endpoint)
        kind = health_kind(endpoint, e)
        if kind is not None:
            self.health.failed(endpoint, kind)

    async def _token_call(self, path: str, body: dict, *, refreshing: bool = False) -> None:
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
        if r.status_code == 401 or (refreshing and r.status_code in (400, 403)):
            # a refused refresh (usually 400 invalid_grant) means: log in again
            raise AhLoginExpired("login expired", r.status_code)
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
        """Single-flight and shielded: concurrent callers share one refresh POST, and a
        cancelled caller leaves it running until the new tokens are saved (AH may have
        rotated the refresh token already, so dropping the answer would lose the login).
        """
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._do_refresh())
            # retrieve the outcome even when every caller was cancelled
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._refresh_task = task
        await asyncio.shield(task)

    async def _do_refresh(self) -> None:
        t = self.store.load()
        if t is None:
            raise AhLoginExpired("not logged in", 401)
        await self._token_call(
            "/mobile-auth/v1/auth/token/refresh",
            {"clientId": "appie-ios", "refreshToken": t.refresh_token},
            refreshing=True,
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
        r = await self.request_raw(method, path, json=json, order_id=order_id)
        if self._is_failure(r.status):
            raise AhApiError(f"{method} {path}: HTTP {r.status}", r.status)
        if r.body is None:
            return None
        if not r.is_json:
            raise AhNotJson(f"{method} {path}: response is not JSON", r.status)
        return r.body

    @staticmethod
    def _is_failure(status: int) -> bool:
        return status >= 400 or 300 <= status < 400

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        order_id: int | None = None,
    ) -> RawResponse:
        """Send a request; returns status and body for every status except 401."""
        await self.ensure_fresh()
        t = self.store.load()
        if t is None:  # gone (or unreadable) since ensure_fresh: log in again
            raise AhLoginExpired(f"{method} {path}: not logged in", 401)
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
        if not r.content:
            return RawResponse(r.status_code, None, False, "")
        try:
            return RawResponse(r.status_code, r.json(), True, r.text)
        except ValueError:
            return RawResponse(r.status_code, r.text, False, r.text)

    async def graphql(self, query: str, variables: dict) -> dict:
        body = await self.request(
            "POST", "/graphql", json={"query": query, "variables": variables}
        )
        if body is not None and not isinstance(body, dict):
            raise AhShapeError("graphql: antwoord is geen object", 200)
        if isinstance(body, dict) and body.get("errors"):
            # a renamed or removed field shows up here: count it as a change
            raise AhShapeError("graphql returned errors", 200)
        data = (body or {}).get("data") or {}
        if not isinstance(data, dict):
            raise AhShapeError("graphql: data is geen object", 200)
        return data

    # --- bonus ---

    async def bonus_periods(self) -> list[BonusPeriod]:
        return await self._track("bonus", self._bonus_periods())

    async def _bonus_periods(self) -> list[BonusPeriod]:
        body = await self.request("GET", "/mobile-services/bonuspage/v3/metadata")
        out = []
        for p in _list_of(body, "periods", "bonus metadata"):
            cats: list[str] = []
            for tab in p.get("tabs") or []:
                for u in tab.get("urlMetadataList") or []:
                    if u.get("bonusType") != "NATIONAL":
                        continue
                    q = parse_qs(urlparse(u.get("url") or "").query)
                    name = (q.get("category") or [u.get("description")])[0]
                    if name and name not in cats:
                        cats.append(name)
            out.append(
                BonusPeriod(
                    start=date.fromisoformat(p["bonusStartDate"]),
                    end=date.fromisoformat(p["bonusEndDate"]),
                    next_visible_from=_iso(p.get("nextPeriodVisibleFrom")),
                    categories=cats,
                )
            )
        return out

    async def _expand_group(self, group: dict, period: BonusPeriod) -> list[Product]:
        gid = str(group["id"])
        name = html.unescape(group.get("segmentDescription") or "") or None
        inline = group.get("products") or []
        if inline:
            return [replace(product_from_card(c), group_id=gid, group_title=name)
                    for c in inline if c.get("webshopId")]
        await self._pause()
        data = await self.graphql(
            GROUP_QUERY,
            {
                "id": str(group["id"]),
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
            },
        )
        out = []
        picture = _widest_image(group.get("images"))  # AH's combined picture of the deal
        for promo in data.get("bonusPromotions") or []:
            for p in promo.get("products") or []:
                if p.get("id") is not None:
                    out.append(
                        product_from_graphql(
                            p, is_bonus=True, category=group.get("category") or None,
                            image=picture,
                        )
                    )
        return [replace(p, group_id=gid, group_title=name) for p in out]

    @staticmethod
    def _is_blocking(e: Exception) -> bool:
        """Errors that mean AH is refusing us: stop at once instead of skipping."""
        return is_blocking(e)

    async def _pause(self) -> None:
        if self._called_ah and CALL_DELAY > 0:
            await asyncio.sleep(CALL_DELAY)
        self._called_ah = True

    async def bonus_offers(self, period: BonusPeriod) -> list[Product]:
        """The whole folder. One broken category or group is skipped (and listed); only
        the folder as a whole counts for ApiHealth."""
        return await self._track("bonus", self._bonus_offers(period))

    async def _bonus_offers(self, period: BonusPeriod) -> list[Product]:
        self.skipped_categories = []
        self.skipped_groups = []
        self._called_ah = False
        groups: dict[str, list[Product] | None] = {}
        found: dict[int, Product] = {}
        failed_in_row = 0
        last_shape = False  # the last skipped category failed on its shape
        for cat in period.categories:
            path = (
                "/mobile-services/bonuspage/v2/section?application=AHWEBSHOP"
                f"&date={period.start.isoformat()}&promotionType=NATIONAL"
                f"&category={quote(cat, safe='')}"
            )
            try:
                await self._pause()
                body = await self.request("GET", path)
                items: list[Product] = []
                for entry in _list_of(body, "bonusGroupOrProducts", "bonus section"):
                    if entry.get("product"):
                        items.append(product_from_card(entry["product"]))
                    elif entry.get("bonusGroup"):
                        g = entry["bonusGroup"]
                        gid = str(g.get("id"))
                        if gid not in groups:
                            try:
                                groups[gid] = await self._expand_group(g, period)
                            except (AhApiError, KeyError, ValueError, TypeError) as e:
                                if self._is_blocking(e):
                                    raise
                                groups[gid] = None
                                self.skipped_groups.append(gid)
                        items.extend(groups[gid] or [])
            except (AhApiError, KeyError, ValueError, TypeError) as e:
                if self._is_blocking(e):
                    raise
                self.skipped_categories.append(cat)
                failed_in_row += 1
                last_shape = isinstance(e, (AhShapeError, *_SHAPE))
                if failed_in_row >= MAX_CONSECUTIVE_CATEGORY_FAILURES:
                    cls = AhShapeError if last_shape else AhApiError
                    raise cls(
                        "bonusfolder niet op te halen: "
                        f"{failed_in_row} categorieen achter elkaar mislukt",
                        getattr(e, "status", 0),
                    ) from None
                continue
            failed_in_row = 0
            for p in items:
                found.setdefault(p.id, p)
        if period.categories and len(self.skipped_categories) == len(period.categories):
            cls = AhShapeError if last_shape else AhApiError
            raise cls("bonusfolder niet op te halen: alle categorieen mislukt", 0)
        return list(found.values())

    async def previously_bought_bonus(self, period: BonusPeriod) -> list[Product]:
        """AH's "Eerder gekocht" bonus section: offers on products bought before."""
        path = (
            "/mobile-services/bonuspage/v2/section/previously-bought"
            f"?application=AHWEBSHOP&date={period.start.isoformat()}"
        )
        self._called_ah = False
        await self._pause()
        body = await self.request("GET", path)
        found: dict[int, Product] = {}
        for entry in (body or {}).get("bonusGroupOrProducts") or []:
            if entry.get("product") and entry["product"].get("webshopId"):
                items = [product_from_card(entry["product"])]
            elif entry.get("bonusGroup"):
                items = await self._expand_group(entry["bonusGroup"], period)
            else:
                items = []
            for p in items:
                found.setdefault(p.id, p)
        return list(found.values())

    # --- purchase history ---

    async def purchases(self, since: date) -> list["Purchase"]:
        """Delivered orders and till receipts from `since` on. Only GET and GraphQL queries.

        One Purchase per product per order or receipt. A receipt line knows only the hq
        id; when that product was also delivered, its webshop id is filled in.
        """
        from .staples import Purchase  # staples imports Product from here

        self._called_ah = False
        out: list[Purchase] = []
        hq_to_ws: dict[int, int] = {}
        await self._pause()
        data = await self.graphql(FULFILLMENTS_QUERY, {})
        for f in (data.get("orderFulfillments") or {}).get("result") or []:
            day = _iso(((f.get("delivery") or {}).get("slot") or {}).get("date"))
            if day is None or day < since or not f.get("orderId"):
                continue
            await self._pause()
            body = await self.request(
                "GET",
                f"/mobile-services/order/v1/{int(f['orderId'])}/details-grouped-by-taxonomy",
            )
            if not isinstance(body, dict) or body.get("orderState") != DELIVERED:
                continue
            day = _iso(body.get("deliveryDate")) or day
            lines: dict[int, Purchase] = {}
            for g in body.get("groupedProductsInTaxonomy") or []:
                for op in g.get("orderedProducts") or []:
                    prod = op.get("product") or {}
                    ws = _pos_int(prod.get("webshopId"))
                    qty = op.get("allocatedQuantity", op.get("quantity"))
                    if ws is None or not isinstance(qty, int) or qty <= 0:
                        continue
                    hq = _pos_int(prod.get("hqId"))
                    if hq is not None:
                        hq_to_ws[hq] = ws
                    if ws in lines:
                        lines[ws].qty += qty
                    else:
                        lines[ws] = Purchase(ws, html.unescape(prod.get("title") or ""), qty,
                                             day, "online", hq_id=hq)
            out.extend(lines.values())

        await self._pause()
        data = await self.graphql(RECEIPTS_QUERY, {"limit": RECEIPTS_LIMIT})
        for r in (data.get("posReceiptsPage") or {}).get("posReceipts") or []:
            day = _local_day(r.get("dateTime"))
            if day is None or day < since or not r.get("id"):
                continue
            await self._pause()
            d = (await self.graphql(RECEIPT_QUERY, {"id": str(r["id"])})).get("posReceiptDetails")
            lines = {}
            for x in (d or {}).get("products") or []:
                hq, qty = _pos_int(x.get("id")), x.get("quantity")
                if hq is None or not isinstance(qty, int) or qty <= 0:
                    continue
                if hq in lines:
                    lines[hq].qty += qty
                else:
                    lines[hq] = Purchase(0, x.get("name") or "", qty, day, "winkel", hq_id=hq)
            out.extend(lines.values())
        for p in out:
            if p.product_id == 0 and p.hq_id in hq_to_ws:
                p.product_id = hq_to_ws[p.hq_id]
        return out

    # --- v1.0: the delivered order of a menu week, read only ---

    def _delivered_from(self, order_id: int, body: Any, day: date | None) -> DeliveredOrder | None:
        if not isinstance(body, dict):
            raise AhShapeError("bestelling: geen object", 200)
        state = body.get("orderState")
        if state != DELIVERED:
            if isinstance(state, str) and "CANCEL" in state.upper():
                return None  # cancelled: final, never delivered
            # open, in delivery, no state at all: not delivered yet, look again later
            return DeliveredOrder(order_id, _iso(body.get("deliveryDate")) or day, {},
                                  state=state if isinstance(state, str) else "ONBEKEND")
        groups = body.get("groupedProductsInTaxonomy")
        if groups is not None and not isinstance(groups, list):
            raise AhShapeError("bestelling: groupedProductsInTaxonomy is geen lijst", 200)
        items: dict[int, int] = {}
        for g in groups or []:
            for op in g.get("orderedProducts") or []:
                ws = _pos_int((op.get("product") or {}).get("webshopId"))
                qty = op.get("allocatedQuantity", op.get("quantity"))
                if ws is None or not isinstance(qty, int) or qty <= 0:
                    continue
                items[ws] = items.get(ws, 0) + qty
        day = _iso(body.get("deliveryDate")) or day
        if day is None:
            raise AhShapeError("bestelling: geen bezorgdatum", 200)
        return DeliveredOrder(order_id, day, items)

    async def _read_delivered(self, order_id: int, day: date | None = None):
        await self._pause()
        body = await self.request(
            "GET", f"/mobile-services/order/v1/{int(order_id)}/details-grouped-by-taxonomy")
        return self._delivered_from(int(order_id), body, day)

    async def delivered_order(self, order_id: int) -> DeliveredOrder | None:
        """The order with this id as delivered; None when it is cancelled; an order that
        is not delivered yet has `delivered` False. Only a GET. Never counts for
        ApiHealth (Ruling 53): a changed shape raises AhShapeError and nothing else."""
        self._called_ah = False
        return await self._untracked("bestelling", self._read_delivered(order_id))

    async def delivered_orders(self, start: date, end: date) -> list[DeliveredOrder]:
        """Every order with its delivery slot from `start` to `end` that is delivered or
        not delivered yet (`delivered` False); cancelled ones are left out. Only a
        GraphQL query and GETs."""
        async def read():
            data = await self.graphql(FULFILLMENTS_QUERY, {})
            out = []
            for f in (data.get("orderFulfillments") or {}).get("result") or []:
                day = _iso(((f.get("delivery") or {}).get("slot") or {}).get("date"))
                oid = _pos_int(f.get("orderId"))
                if day is None or oid is None or not start <= day <= end:
                    continue
                got = await self._read_delivered(oid, day)
                if got is not None:
                    out.append(got)
            return out

        self._called_ah = False
        await self._pause()
        return await self._untracked("bestelling", read())

    # --- products ---

    async def search_products(self, query: str, size: int = 20) -> list[Product]:
        return await self._track("product", self._search_products(query, size))

    async def _search_products(self, query: str, size: int) -> list[Product]:
        path = (
            f"/mobile-services/product/search/v2?query={quote(query, safe='')}"
            f"&page=0&size={size}&sortOn=RELEVANCE"
        )
        body = await self.request("GET", path)
        return [
            product_from_card(c)
            for c in _list_of(body, "products", "product search")
            if c.get("webshopId")
        ]

    # --- recipes ---

    async def search_recipes(self, text: str, size: int = 30) -> list[RecipeSummary]:
        return await self._track("recept", self._search_recipes(text, size))

    async def _search_recipes(self, text: str, size: int) -> list[RecipeSummary]:
        data = await self.graphql(
            RECIPE_SEARCH_QUERY, {"query": {"searchText": text, "size": size}}
        )
        out = []
        for r in _list_of(data.get("recipeSearch"), "result", "recipe search"):
            t = r.get("time") or {}
            out.append(
                RecipeSummary(
                    id=int(r["id"]),
                    title=html.unescape(r.get("title") or ""),
                    slug=r.get("slug") or "",
                    courses=list(r.get("courses") or []),
                    cook=_minutes(t.get("cook")),
                    oven=_minutes(t.get("oven")),
                    wait=_minutes(t.get("wait")),
                    servings=int((r.get("serving") or {}).get("number") or 0),
                    image=_widest_image(r.get("images")),
                )
            )
        return out

    async def get_recipe(self, id: int, servings: int) -> Recipe:
        return await self._track("recept", self._get_recipe(id, servings))

    async def _get_recipe(self, id: int, servings: int) -> Recipe:
        data = await self.graphql(RECIPE_QUERY, {"id": id, "servings": servings})
        r = data.get("recipe")
        if not r:
            raise AhApiError(f"recipe {id} not found", 200)
        slug = (r.get("href") or "").rstrip("/").rsplit("/", 1)[-1]
        return Recipe(
            id=int(r["id"]),
            title=html.unescape(r.get("title") or ""),
            url=f"https://www.ah.nl/allerhande/recept/R-R{int(r['id'])}/{slug}",
            cook=_minutes(r.get("cookTime")),
            oven=None,  # only on search results; the planner copies it from RecipeSummary
            wait=None,
            courses=list(r.get("courses") or []),
            cuisines=list(r.get("cuisines") or []),
            servings=int((r.get("servings") or {}).get("number") or servings),
            ingredients=[
                (
                    html.unescape(i.get("text") or ""),
                    html.unescape((i.get("name") or {}).get("singular") or ""),
                )
                for i in r.get("ingredients") or []
            ],
            steps=[html.unescape(x) for x in (r.get("preparation") or {}).get("steps") or []],
            image=_widest_image(r.get("images")),
        )

    # --- shopping list ---

    async def shopping_list(self) -> dict[int, int]:
        return (await self._list_state())[0]

    async def _list_state(self) -> tuple[dict[int, int], set[int]]:
        """Quantities per product, and the products with a struck-through (crossed off) line."""
        return await self._track("lijst", self._read_list())

    async def _read_list(self) -> tuple[dict[int, int], set[int]]:
        path = "/mobile-services/shoppinglist/v2/items"
        r = await self.request_raw("GET", path)
        if ORDER_MODE_TEXT in r.text:
            raise AhOrderMode("shopping list is locked: order mode", r.status)
        if self._is_failure(r.status):
            raise AhApiError(f"GET {path}: HTTP {r.status}", r.status)
        if not r.is_json or not isinstance(r.body, dict):
            raise AhNotJson(f"GET {path}: response is not JSON", r.status)
        out: dict[int, int] = {}
        struck: set[int] = set()
        for it in _list_of(r.body, "items", "shopping list"):
            pid = ((it.get("productDetails") or {}).get("product") or {}).get("webshopId")
            if pid:
                pid = _int(pid, "shopping list webshopId")
                qty = it.get("quantity")
                out[pid] = out.get(pid, 0) + (0 if qty is None else _int(qty, "list quantity"))
                if it.get("strikedthrough") or it.get("strikeThrough"):
                    struck.add(pid)
        return out, struck

    async def add_to_shopping_list(
        self, adds: dict[int, int], on_before: Callable[[dict[int, int]], None] | None = None
    ) -> None:
        """Never retried. The PATCH sets quantities, so send current + added.

        `on_before` gets the fresh current quantities of the added products
        right before the PATCH (write-ahead hook).
        """
        if not adds:
            return
        current = await self.shopping_list()
        items = []
        for pid, qty in adds.items():
            if qty <= 0:
                raise ValueError("adds must be positive")
            items.append(_list_item(pid, current.get(pid, 0) + qty))
        if on_before is not None:
            on_before({pid: current.get(pid, 0) for pid in adds})
        await self._track("lijst:schrijven", self.request(
            "PATCH", "/mobile-services/shoppinglist/v2/items", json={"items": items}
        ))

    async def reduce_on_shopping_list(
        self, reductions: dict[int, int],
        on_before: Callable[[dict[int, int]], None] | None = None,
    ) -> set[int]:
        """Never retried. Lower each product by its reduction, never below 0 (0 takes
        it off the list) and never higher: the PATCH sets max(0, current - reduction).

        A product with a struck-through (crossed off) line is never touched: it is
        skipped and returned, so a crossed-off line is never un-crossed. `on_before`
        gets the fresh current quantities of the other products right before the
        PATCH (not called when every product is skipped). A product already at 0 is
        not sent; with nothing to lower there is no PATCH.
        """
        _check_reductions(reductions)
        if not reductions:
            return set()
        current, struck = await self._list_state()
        skipped = {pid for pid in reductions if pid in struck}
        todo = {pid: r for pid, r in reductions.items() if pid not in skipped}
        if not todo:
            return skipped
        items = [
            _list_item(pid, max(0, current.get(pid, 0) - r))
            for pid, r in todo.items() if current.get(pid, 0) > 0
        ]
        if on_before is not None:
            on_before({pid: current.get(pid, 0) for pid in todo})
        if items:
            await self._track("lijst:schrijven", self.request(
                "PATCH", "/mobile-services/shoppinglist/v2/items", json={"items": items}
            ))
        return skipped

    # --- order ---

    async def active_order(self) -> ActiveOrder | None:
        return await self._track("bestelling", self._read_order())

    async def _read_order(self) -> ActiveOrder | None:
        path = "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT"
        r = await self.request_raw("GET", path)
        if r.status == 404:
            log.info("Bestelling lezen (doel: waar de producten heen gaan): HTTP 404, "
                     "geen actieve bestelling, lijst gebruikt")
            return None
        if self._is_failure(r.status):
            raise AhApiError(f"GET {path}: HTTP {r.status}", r.status)
        if not r.is_json or not isinstance(r.body, dict):
            raise AhNotJson(f"GET {path}: response is not an order", r.status)
        if "id" not in r.body:
            raise AhShapeError(f"GET {path}: bestelling zonder id", r.status)
        ordered = r.body.get("orderedProducts")
        if ordered is not None and not isinstance(ordered, list):
            raise AhShapeError(f"GET {path}: orderedProducts is geen lijst", r.status)
        items: dict[int, int] = {}
        for op in ordered or []:
            pid = (op.get("product") or {}).get("webshopId")
            if pid:
                pid = _int(pid, "order webshopId")
                qty = op.get("quantity")
                items[pid] = items.get(pid, 0) + (0 if qty is None else _int(qty, "order quantity"))
        body = r.body
        day = (_local_day(body.get("deliveryDate"))
               or _local_day(((body.get("delivery") or {}).get("slot") or {}).get("date")))
        return ActiveOrder(id=_int(body["id"], "order id"), items=items, delivery_date=day)

    async def order_delivery_date(self, order: ActiveOrder) -> date | None:
        """The day `order` is delivered, or None when that cannot be known.

        From the summary when it names the day, else from the open orders
        (GraphQL orderFulfillments(status: OPEN)) by order id. Any failure but an
        expired login gives None: the caller then writes nothing (never a guess).
        """
        if order.delivery_date is not None:
            return order.delivery_date
        try:
            data = await self.graphql(OPEN_FULFILLMENTS_QUERY, {})
        except AhLoginExpired:
            raise
        except Exception as e:
            log.warning("Bezorgdatum van de bestelling niet te lezen (%s)", type(e).__name__)
            return None
        for f in (data.get("orderFulfillments") or {}).get("result") or []:
            if _pos_int(f.get("orderId")) == order.id:
                return _local_day(((f.get("delivery") or {}).get("slot") or {}).get("date"))
        return None

    async def add_to_order(
        self,
        order: ActiveOrder,
        adds: dict[int, int],
        on_before: Callable[[dict[int, int]], None] | None = None,
    ) -> None:
        """Never retried. The PUT sets quantities, so send current + added.

        The current quantities are re-read right before the PUT, so changes
        made in the app since `order` was read are never lowered. `on_before`
        gets those fresh quantities of the added products right before the PUT.
        """
        if not adds:
            return
        for qty in adds.values():
            if qty <= 0:
                raise ValueError("adds must be positive")
        fresh = await self.active_order()
        if fresh is None or fresh.id != order.id:
            raise AhApiError("bestelling is niet meer actief", 404)
        items = []
        for pid, qty in adds.items():
            items.append(_order_item(pid, fresh.items.get(pid, 0) + qty))
        if on_before is not None:
            on_before({pid: fresh.items.get(pid, 0) for pid in adds})
        await self._track("bestelling:schrijven", self.request(
            "PUT",
            "/mobile-services/order/v1/items?sortBy=DEFAULT",
            json={"items": items},
            order_id=order.id,
        ))

    async def reduce_in_order(
        self,
        order: ActiveOrder,
        reductions: dict[int, int],
        on_before: Callable[[dict[int, int]], None] | None = None,
    ) -> None:
        """Never retried. Lower each product by its reduction, never below 0 (0 takes
        it out of the order) and never higher: the PUT sets max(0, current - reduction).

        The order is re-read right before the PUT; another order id writes nothing.
        `on_before` gets those fresh quantities. A product already at 0 is not sent;
        with nothing to lower there is no PUT.
        """
        _check_reductions(reductions)
        if not reductions:
            return
        fresh = await self.active_order()
        if fresh is None or fresh.id != order.id:
            raise AhApiError("bestelling is niet meer actief", 404)
        items = [
            _order_item(pid, max(0, fresh.items.get(pid, 0) - r))
            for pid, r in reductions.items() if fresh.items.get(pid, 0) > 0
        ]
        if on_before is not None:
            on_before({pid: fresh.items.get(pid, 0) for pid in reductions})
        if items:
            await self._track("bestelling:schrijven", self.request(
                "PUT",
                "/mobile-services/order/v1/items?sortBy=DEFAULT",
                json={"items": items},
                order_id=order.id,
            ))
