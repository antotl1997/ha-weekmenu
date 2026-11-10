"""In-memory fakes for the round and the web API (no network)."""

import asyncio
from datetime import date, datetime

import httpx

from app.ahclient import ActiveOrder, AhApiError, BonusPeriod, Product, Recipe
from app.hass import HELPER_AAN, HELPER_AVONDEN, Hass, helper_persons
from app.ledger import Ledger
from app.main import Context
from app.planner import Dish, Line, Plan
from app.tokens import TokenStore

# Menu week of Wednesday 4 November 2026; Sunday 1 November is the schedule day.
W = date(2026, 11, 4)
SUNDAY = datetime(2026, 11, 1, 8, 0)
CURRENT = BonusPeriod(date(2026, 10, 26), date(2026, 11, 1), None, ["Vlees"])
NEXT = BonusPeriod(date(2026, 11, 2), date(2026, 11, 8), None, ["Vlees"])


def prod(pid: int, bonus: bool = True) -> Product:
    return Product(pid, f"Product {pid}", None, 5.0, 3.0, bonus,
                   "2e halve prijs" if bonus else None, True, None)


def recipe(rid: int) -> Recipe:
    return Recipe(rid, f"Recept {rid}", f"https://example.invalid/r/{rid}", 30, None, None,
                  ["hoofdgerecht"], [], 2, [("1 stuk ui", "ui")], ["Snijd de ui."],
                  f"https://example.invalid/{rid}.jpg")


def dish(day: date, rid: int, pids, savings: float = 1.0, persons: int = 2) -> Dish:
    return Dish(day, recipe(rid), "pasta", False,
                [Line(prod(p), 1, f"ingredient {p}") for p in pids], savings,
                persons=persons, cook=30, pantry=["1 el olie"])


def make_plan(dishes, short_by: int = 0) -> Plan:
    basket: dict[int, int] = {}
    products: dict[int, Product] = {}
    for d in dishes:
        for ln in d.lines:
            basket[ln.product.id] = basket.get(ln.product.id, 0) + ln.qty
            products[ln.product.id] = ln.product
    return Plan(list(dishes), basket, products, round(sum(d.savings for d in dishes), 2),
                [], short_by, pantry=["olie", "zout"])


class FakeAh:
    """AH in memory: real quantities, every write recorded, optional failure.

    fail_on_call=N: write N (0-based) fails; fail_after=True: AH applied it first.
    """

    def __init__(self, tmp_path, periods=(CURRENT, NEXT), order=None,
                 fail_on_call=None, fail_after=False, exc=None):
        self.store = TokenStore(tmp_path / "tokens.json")
        self.periods = list(periods)
        self.order = order
        self.listed: dict[int, int] = {}
        self.writes: list[tuple[str, dict]] = []
        self.calls: list[str] = []
        self.offers_for: list[BonusPeriod] = []
        self.fail_on_call = fail_on_call
        self.fail_after = fail_after
        self.exc = exc or AhApiError("boom", 500)
        self.raise_on: dict[str, Exception] = {}
        self.codes: list[str] = []
        self.active_writes = 0
        self.max_active_writes = 0
        self.history: list = []  # Purchase list returned by purchases()
        self.prev_bonus: list[Product] = []  # previously_bought_bonus()
        self.history_for: list = []
        self.fresh_checks = 0

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self.raise_on:
            raise self.raise_on[name]

    async def ensure_fresh(self):
        # not in `calls`: tests compare that list with the AH reads of a round
        if "ensure_fresh" in self.raise_on:
            raise self.raise_on["ensure_fresh"]
        self.fresh_checks += 1

    async def bonus_periods(self):
        self._maybe_raise("bonus_periods")
        return list(self.periods)

    async def bonus_offers(self, period):
        self._maybe_raise("bonus_offers")
        self.offers_for.append(period)
        return [prod(1), prod(2)]

    async def purchases(self, since):
        self._maybe_raise("purchases")
        self.history_for.append(since)
        return list(self.history)

    async def previously_bought_bonus(self, period):
        self._maybe_raise("previously_bought_bonus")
        return list(self.prev_bonus)

    async def get_recipe(self, id, servings):
        self._maybe_raise("get_recipe")
        return recipe(id)

    async def exchange_code(self, code):
        self._maybe_raise("exchange_code")
        self.codes.append(code)
        from app.tokens import Tokens
        self.store.save(Tokens("secret-access-token", "secret-refresh-token", 9e9))

    async def active_order(self):
        if "active_order" in self.raise_on:  # not in `calls`, like ensure_fresh
            raise self.raise_on["active_order"]
        if self.order is None:
            return None
        return ActiveOrder(self.order.id, dict(self.order.items))

    async def shopping_list(self):
        return dict(self.listed)

    async def _write(self, kind, store, adds, on_before):
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            await asyncio.sleep(0.01)  # let other tasks interleave if they can
            (pid, qty), = adds.items()
            if on_before:
                on_before({pid: store.get(pid, 0)})
            failing = self.fail_on_call is not None and len(self.writes) == self.fail_on_call
            self.writes.append((kind, dict(adds)))
            if failing and not self.fail_after:
                raise self.exc
            store[pid] = store.get(pid, 0) + qty
            if failing:
                raise self.exc
        finally:
            self.active_writes -= 1

    async def add_to_order(self, order, adds, on_before=None):
        await self._write("order", self.order.items, adds, on_before)

    async def add_to_shopping_list(self, adds, on_before=None):
        await self._write("list", self.listed, adds, on_before)

    async def _reduce(self, kind, store, cuts, on_before):
        """Like AH: max(0, current - r). Recorded in `writes` as a negative quantity."""
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            await asyncio.sleep(0.01)
            (pid, r), = cuts.items()
            assert r > 0
            if on_before:
                on_before({pid: store.get(pid, 0)})
            failing = self.fail_on_call is not None and len(self.writes) == self.fail_on_call
            self.writes.append((kind, {pid: -r}))
            if failing and not self.fail_after:
                raise self.exc
            left = max(0, store.get(pid, 0) - r)
            if left:
                store[pid] = left
            else:
                store.pop(pid, None)
            if failing:
                raise self.exc
        finally:
            self.active_writes -= 1

    async def reduce_in_order(self, order, cuts, on_before=None):
        await self._reduce("order", self.order.items, cuts, on_before)

    async def reduce_on_shopping_list(self, cuts, on_before=None):
        await self._reduce("list", self.listed, cuts, on_before)

    def held(self) -> dict[int, int]:
        """What AH holds now, list and order together."""
        out = dict(self.listed)
        if self.order is not None:
            for p, n in self.order.items.items():
                out[p] = out.get(p, 0) + n
        return out


class FakeHass(Hass):
    """The real Hass logic on top of an in-memory state table; posts are recorded."""

    def __init__(self, total_path, enabled=True, aantal=5,
                 fail_posts=False):
        super().__init__(http=None, total_path=total_path)
        self.states = {
            HELPER_AAN: "on" if enabled else "off",
            HELPER_AVONDEN: str(aantal),
            **{eid: "2" for eid in helper_persons().values()},
        }
        self.posts: list[tuple[str, dict]] = []
        self.fail_posts = fail_posts
        self.fail_reads = False

    async def _get_state(self, entity_id):
        if self.fail_reads:
            raise httpx.ConnectError("HA is weg")
        return self.states.get(entity_id)

    async def _post(self, path, payload):
        self.posts.append((path, payload))
        if self.fail_posts:
            raise httpx.ConnectError("HA is weg")

    def notifications(self):
        return [p for path, p in self.posts if path.startswith("services/notify/")]

    def totals(self):
        return [p["state"] for path, p in self.posts
                if path == "states/sensor.weekmenu_besparing_totaal"]


def make_ctx(tmp_path, ah=None, hass=None, now=SUNDAY, **kw) -> Context:
    ah = ah or FakeAh(tmp_path)
    hass = hass or FakeHass(tmp_path / "totaal.json")
    ctx = Context(
        client=ah,
        ledger=Ledger(tmp_path / "weeks"),
        hass=hass,
        store=ah.store,
        pantry=["olie", "zout", "peper", "suiker", "bloem", "azijn"],
        total_path=tmp_path / "totaal.json",
        state_path=tmp_path / "state.json",
        **kw,
    )
    ctx.clock = lambda: now
    return ctx
