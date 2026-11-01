import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import httpx

from .tokens import Tokens, TokenStore

if TYPE_CHECKING:
    from .staples import Purchase

BASE_URL = "https://api.ah.nl"
LOGIN_URL = (
    "https://login.ah.nl/login?client_id=appie-ios"
    "&response_type=code&redirect_uri=appie://login-exit"
)
# Bump together when AH refuses old app versions.
APP_VERSION = "9.28"
USER_AGENT = f"Appie/{APP_VERSION} (iPhone17,3; iPhone; CPU OS 26_1 like Mac OS X)"
REFRESH_MARGIN = 300


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
class ActiveOrder:
    id: int
    items: dict[int, int] = field(default_factory=dict)


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
    )


def product_from_graphql(
    p: dict, is_bonus: bool = True, category: str | None = None
) -> Product:
    """GraphQL product (bonus group expansion)."""
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


class AhClient:
    def __init__(self, store: TokenStore, http: httpx.AsyncClient):
        self.store = store
        self.http = http
        self.skipped_categories: list[str] = []
        self.skipped_groups: list[str] = []
        self._called_ah = False

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
        if isinstance(body, dict) and body.get("errors"):
            raise AhApiError("graphql returned errors", 200)
        return (body or {}).get("data", {})

    # --- bonus ---

    async def bonus_periods(self) -> list[BonusPeriod]:
        body = await self.request("GET", "/mobile-services/bonuspage/v3/metadata")
        out = []
        for p in (body or {}).get("periods") or []:
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
        inline = group.get("products") or []
        if inline:
            return [product_from_card(c) for c in inline if c.get("webshopId")]
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
        for promo in data.get("bonusPromotions") or []:
            for p in promo.get("products") or []:
                if p.get("id") is not None:
                    # GraphQL has no category: use the group's own (the planner filters on it)
                    out.append(
                        product_from_graphql(
                            p, is_bonus=True, category=group.get("category") or None
                        )
                    )
        return out

    @staticmethod
    def _is_blocking(e: Exception) -> bool:
        """Errors that mean AH is refusing us: stop at once instead of skipping."""
        return isinstance(e, (AhLoginExpired, AhNotJson)) or (
            isinstance(e, AhApiError) and e.status == 429
        )

    async def _pause(self) -> None:
        if self._called_ah and CALL_DELAY > 0:
            await asyncio.sleep(CALL_DELAY)
        self._called_ah = True

    async def bonus_offers(self, period: BonusPeriod) -> list[Product]:
        self.skipped_categories = []
        self.skipped_groups = []
        self._called_ah = False
        groups: dict[str, list[Product] | None] = {}
        found: dict[int, Product] = {}
        failed_in_row = 0
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
                for entry in (body or {}).get("bonusGroupOrProducts") or []:
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
                if failed_in_row >= MAX_CONSECUTIVE_CATEGORY_FAILURES:
                    raise AhApiError(
                        "bonusfolder niet op te halen: "
                        f"{failed_in_row} categorieen achter elkaar mislukt",
                        getattr(e, "status", 0),
                    ) from None
                continue
            failed_in_row = 0
            for p in items:
                found.setdefault(p.id, p)
        if period.categories and len(self.skipped_categories) == len(period.categories):
            raise AhApiError("bonusfolder niet op te halen: alle categorieen mislukt", 0)
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

    # --- products ---

    async def search_products(self, query: str, size: int = 20) -> list[Product]:
        path = (
            f"/mobile-services/product/search/v2?query={quote(query, safe='')}"
            f"&page=0&size={size}&sortOn=RELEVANCE"
        )
        body = await self.request("GET", path)
        return [
            product_from_card(c)
            for c in (body or {}).get("products") or []
            if c.get("webshopId")
        ]

    # --- recipes ---

    async def search_recipes(self, text: str, size: int = 30) -> list[RecipeSummary]:
        data = await self.graphql(
            RECIPE_SEARCH_QUERY, {"query": {"searchText": text, "size": size}}
        )
        out = []
        for r in ((data.get("recipeSearch") or {}).get("result")) or []:
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
        path = "/mobile-services/shoppinglist/v2/items"
        r = await self.request_raw("GET", path)
        if ORDER_MODE_TEXT in r.text:
            raise AhOrderMode("shopping list is locked: order mode", r.status)
        if self._is_failure(r.status):
            raise AhApiError(f"GET {path}: HTTP {r.status}", r.status)
        if not r.is_json or not isinstance(r.body, dict):
            raise AhNotJson(f"GET {path}: response is not JSON", r.status)
        out: dict[int, int] = {}
        for it in r.body.get("items") or []:
            pid = ((it.get("productDetails") or {}).get("product") or {}).get("webshopId")
            if pid:
                out[int(pid)] = out.get(int(pid), 0) + int(it.get("quantity") or 0)
        return out

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
            items.append(
                {
                    "description": "",
                    "productId": pid,
                    "quantity": current.get(pid, 0) + qty,
                    "type": "SHOPPABLE",
                    "originCode": "PRD",
                    "strikeThrough": False,
                }
            )
        if on_before is not None:
            on_before({pid: current.get(pid, 0) for pid in adds})
        await self.request(
            "PATCH", "/mobile-services/shoppinglist/v2/items", json={"items": items}
        )

    # --- order ---

    async def active_order(self) -> ActiveOrder | None:
        path = "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT"
        r = await self.request_raw("GET", path)
        if r.status == 404:
            return None
        if self._is_failure(r.status):
            raise AhApiError(f"GET {path}: HTTP {r.status}", r.status)
        if not r.is_json or not isinstance(r.body, dict) or "id" not in r.body:
            raise AhNotJson(f"GET {path}: response is not an order", r.status)
        items: dict[int, int] = {}
        for op in r.body.get("orderedProducts") or []:
            pid = (op.get("product") or {}).get("webshopId")
            if pid:
                items[int(pid)] = items.get(int(pid), 0) + int(op.get("quantity") or 0)
        return ActiveOrder(id=int(r.body["id"]), items=items)

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
            items.append(
                {
                    "productId": pid,
                    "quantity": fresh.items.get(pid, 0) + qty,
                    "originCode": "PRD",
                    "description": "",
                    "strikethrough": False,
                }
            )
        if on_before is not None:
            on_before({pid: fresh.items.get(pid, 0) for pid in adds})
        await self.request(
            "PUT",
            "/mobile-services/order/v1/items?sortBy=DEFAULT",
            json={"items": items},
            order_id=order.id,
        )
