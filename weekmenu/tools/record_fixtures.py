"""Record raw AH responses outside the repo: python -m tools.record_fixtures

Read-only: GET requests and GraphQL queries only. Writes to
%APPDATA%\\weekmenu-test\\raw\\. Request headers are never written.

Contract check (v0.7, local only, never in CI: CI has no AH login and no AH data):
python -m tools.record_fixtures --check <path to tokens.json>. Use a login of your own
(log in once for the tool), never a copy of the add-on's tokens.json: AH rotates the
refresh token, so a refresh here would log the add-on out. The check never refreshes:
with an access token that has (almost) expired it stops and asks for a fresh login.
It makes a fresh read-only
recording of the critical endpoints, keeps it in memory, and compares its shape (keys and
types, recursively, lists on their first element) with the stored fixtures. It prints
the differences, never a value, and exits 1 when there are any.
"""
import argparse
import asyncio
import sys
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from app.ahclient import BASE_URL, REFRESH_MARGIN, AhClient
from app.tokens import TokenStore

ROOT = Path(os.environ.get("APPDATA", ".")) / "weekmenu-test"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
RAW = ROOT / "raw"
DELAY = 0.3
SEARCHES = ["kip", "gehakt", "zalm", "pasta", "stamppot", "curry", "wraps", "ovenschotel"]
IMAGES = "images { url width height }"

PRODUCT_FIELDS = (
    "id title brand salesUnitSize priceV2 { now { amount } was { amount } "
    "discount { description } }"
)
Q_GROUP = (
    "query BonusGroupProducts($id: String, $start: String, $end: String) {\n"
    "  bonusPromotions(input: {id: $id, periodStart: $start, periodEnd: $end}) {\n"
    f"    products {{ {PRODUCT_FIELDS} }}\n  }}\n}}"
)
Q_SEARCH = (
    "query RecipeSearch($query: RecipeSearchParams!) {\n"
    "  recipeSearch(query: $query) {\n    page { total }\n"
    "    result { id title slug nutriScore courses time { cook oven wait } "
    "serving { number type } rating { average count } %s}\n  }\n}"
)
Q_RECIPE = (
    "query Recipe($id: Int!, $servings: Int) {\n"
    "  recipe(id: $id, servings: $servings) {\n"
    "    id title description href cookTime nutriScore courses cuisines\n"
    f"    {IMAGES}\n"
    "    servings { number type }\n    rating { average count }\n"
    "    ingredients { text name { singular } }\n    preparation { steps }\n"
    "    nutritions {\n      energy { value unit name } carbohydrates { value unit name }\n"
    "      fat { value unit name } protein { value unit name }\n    }\n  }\n}"
)
Q_INTROSPECT = (
    '{ __type(name:"Recipe"){fields{name type{name kind ofType{name}}}} }'
)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80] or "x"


class Recorder:
    def __init__(self, client: AhClient, refresh: bool = True):
        self.c = client
        self.refresh = refresh  # False: never rotate the tokens (the contract check)
        self.calls = 0
        self.log: list[str] = []

    async def fetch(self, method: str, path: str, json_body=None):
        """Returns (status, parsed body or text). Never raises on HTTP status."""
        await asyncio.sleep(DELAY)
        if self.refresh:
            await self.c.ensure_fresh()
        headers = self.c._headers()
        headers["Authorization"] = f"Bearer {self.c.store.load().access_token}"
        r = await self.c.http.request(
            method, BASE_URL + path, json=json_body, headers=headers,
            follow_redirects=False,
        )
        self.calls += 1
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:2000]

    def save(self, name: str, data, status: int):
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.log.append(f"{status} {name}")
        print(status, name)

    async def rest(self, endpoint: str, params: str, path: str, wrap: bool = False):
        status, body = await self.fetch("GET", path)
        data = {"status": status, "body": body} if wrap else body
        self.save(f"{endpoint}__{params}", data, status)
        return status, body

    async def gql(self, endpoint: str, params: str, query: str, variables: dict, wrap=False):
        status, body = await self.fetch(
            "POST", "/graphql", {"query": query, "variables": variables}
        )
        data = {"status": status, "body": body} if wrap else body
        self.save(f"graphql-{endpoint}__{params}", data, status)
        return status, body


def categories_of(period: dict) -> list[str]:
    cats: list[str] = []
    for tab in period.get("tabs") or []:
        for m in tab.get("urlMetadataList") or []:
            if m.get("bonusType") == "NATIONAL" and m.get("description"):
                if m["description"] not in cats:
                    cats.append(m["description"])
    return cats


async def record_bonus(rec: Recorder):
    status, meta = await rec.rest(
        "bonus-metadata", "v3", "/mobile-services/bonuspage/v3/metadata"
    )
    if status != 200:
        raise SystemExit(f"bonus metadata gave HTTP {status}, stopping")
    for idx, period in enumerate((meta.get("periods") or [])[:2]):
        start, end = period["bonusStartDate"], period["bonusEndDate"]
        label = f"p{idx}"
        groups_done = 0
        max_groups = 2 if idx == 0 else 1
        for cat in categories_of(period):
            q = urlencode(
                {"application": "AHWEBSHOP", "date": start,
                 "promotionType": "NATIONAL", "category": cat},
                quote_via=quote,
            )
            st, body = await rec.rest(
                "bonus-section", f"{label}-{slug(cat)}",
                f"/mobile-services/bonuspage/v2/section?{q}",
            )
            if st == 200 and groups_done < max_groups:
                for it in body.get("bonusGroupOrProducts") or []:
                    g = it.get("bonusGroup")
                    if g and g.get("id") and groups_done < max_groups:
                        await rec.gql(
                            "bonuspromotions", f"{label}-{slug(g['id'])}", Q_GROUP,
                            {"id": g["id"], "start": start, "end": end},
                        )
                        groups_done += 1


async def record_recipes(rec: Recorder):
    picked: list[int] = []
    per_search = {}
    summary_q = Q_SEARCH % IMAGES
    for text in SEARCHES:
        variables = {"query": {"searchText": text, "size": 30}}
        st, body = await rec.gql("recipesearch", slug(text), summary_q, variables)
        if st == 400:  # images not on the summary type: fall back to the known fields
            st, body = await rec.gql("recipesearch", slug(text), Q_SEARCH % "", variables)
        per_search[text] = ((body.get("data") or {}).get("recipeSearch") or {}).get("result") or []
    # up to 3 main-course recipes per search, round-robin until 20
    for rnd in range(3):
        for text in SEARCHES:
            mains = [
                r for r in per_search[text]
                if any("hoofd" in c.lower() for c in (r.get("courses") or []))
                and r["id"] not in picked
            ]
            if mains and len(picked) < 20:
                picked.append(mains[0]["id"])
    details = {}
    for i, rid in enumerate(picked):
        st, body = await rec.gql(
            "recipe", f"{rid}-s2", Q_RECIPE, {"id": rid, "servings": 2}
        )
        details[rid] = (body.get("data") or {}).get("recipe")
        if i < 2:
            await rec.gql("recipe", f"{rid}-s4", Q_RECIPE, {"id": rid, "servings": 4})
    names: list[str] = []
    for rid in picked[:5]:
        for ing in (details.get(rid) or {}).get("ingredients") or []:
            n = ((ing.get("name") or {}).get("singular") or ing.get("text") or "").strip()
            if n and n.lower() not in [x.lower() for x in names]:
                names.append(n)
    for n in names:
        q = urlencode(
            {"query": n, "page": 0, "size": 20, "sortOn": "RELEVANCE"}, quote_via=quote
        )
        await rec.rest(
            "product-search", slug(n), f"/mobile-services/product/search/v2?{q}"
        )


async def main():
    store = TokenStore(ROOT / "tokens.json")
    async with httpx.AsyncClient(timeout=30) as http:
        rec = Recorder(AhClient(store, http))
        await rec.gql(
            "introspection", "recipe-type", Q_INTROSPECT, {}, wrap=True
        )
        await record_bonus(rec)
        await record_recipes(rec)
        await rec.rest(
            "shoppinglist", "items", "/mobile-services/shoppinglist/v2/items", wrap=True
        )
        await rec.rest(
            "order-active", "default",
            "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT", wrap=True,
        )
        print("total calls:", rec.calls)


# --- contract check ----------------------------------------------------------------------


def _kind(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def compare_shapes(stored, fresh, path: str = "$") -> list[str]:
    """The shape differences between a stored fixture and a fresh answer.

    Keys and types, recursively; a list is compared on its first element (only when
    both have one). null on either side matches anything: AH leaves optional fields
    empty. Int and float are both "number". Never returns a value, only paths and types.
    """
    a, b = _kind(stored), _kind(fresh)
    if a == "null" or b == "null":
        return []
    if a != b:
        return [f"{path}: type {a} -> {b}"]
    out: list[str] = []
    if a == "object":
        for k in stored:
            if k not in fresh:
                out.append(f"{path}.{k}: ontbreekt")
        for k in fresh:
            if k not in stored:
                out.append(f"{path}.{k}: nieuw")
        for k in stored:
            if k in fresh:
                out.extend(compare_shapes(stored[k], fresh[k], f"{path}.{k}"))
    elif a == "list" and stored and fresh:
        out.extend(compare_shapes(stored[0], fresh[0], f"{path}[0]"))
    return out


def _fixture(name: str):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if isinstance(data, dict) and set(data) == {"status", "body"}:
        return data["status"], data["body"]
    return 200, data


def check_one(name: str, status: int, body, stored_status: int, stored) -> list[str]:
    """Differences for one endpoint, each line starting with the fixture name."""
    if status != stored_status:
        return [f"{name}: HTTP {stored_status} -> {status} (vorm niet vergeleken)"]
    return [f"{name}: {d}" for d in compare_shapes(stored, body)]


async def check(tokens_path: Path) -> int:
    store = TokenStore(Path(tokens_path))
    t = store.load()
    if t is None:
        print("Geen bruikbare tokens in", Path(tokens_path).name)
        return 2
    if t.expires_at - time.time() < REFRESH_MARGIN:
        # never refresh here: that could rotate a login someone else still uses
        print("Access token (bijna) verlopen: log opnieuw in voor de check, "
              "verversen doet de check niet")
        return 2
    diffs: list[str] = []
    async with httpx.AsyncClient(timeout=30) as http:
        rec = Recorder(AhClient(store, http), refresh=False)

        async def rest(name, path):
            status, body = await rec.fetch("GET", path)
            diffs.extend(check_one(name, status, body, *_fixture(name)))
            return status, body

        async def gql(name, query, variables):
            status, body = await rec.fetch("POST", "/graphql",
                                           {"query": query, "variables": variables})
            diffs.extend(check_one(name, status, body, *_fixture(name)))

        status, meta = await rest("bonus-metadata__v3.json",
                                  "/mobile-services/bonuspage/v3/metadata")
        periods = (meta.get("periods") or []) if isinstance(meta, dict) else []
        if status == 200 and periods and periods[0].get("bonusStartDate"):
            q = urlencode({"application": "AHWEBSHOP", "date": periods[0]["bonusStartDate"],
                           "promotionType": "NATIONAL", "category": "Vlees"},
                          quote_via=quote)
            await rest("bonus-section__p0-vlees.json",
                       f"/mobile-services/bonuspage/v2/section?{q}")
        await gql("graphql-recipesearch__kip.json", Q_SEARCH % IMAGES,
                  {"query": {"searchText": "kip", "size": 30}})
        recipe = sorted(FIXTURES.glob("graphql-recipe__*-s2.json"))[0]
        rid = int(recipe.name.split("__")[1].split("-")[0])
        await gql(recipe.name, Q_RECIPE, {"id": rid, "servings": 2})
        q = urlencode({"query": "biologische citroen", "page": 0, "size": 20,
                       "sortOn": "RELEVANCE"}, quote_via=quote)
        await rest("product-search__biologische-citroen.json",
                   f"/mobile-services/product/search/v2?{q}")
        await rest("shoppinglist__items.json", "/mobile-services/shoppinglist/v2/items")
        status, body = await rec.fetch(
            "GET", "/mobile-services/order/v1/summaries/active?sortBy=DEFAULT")
        name = ("order-active__default.json" if status == 404
                else "handmade-order-active__items.json")
        diffs.extend(check_one(name, status, body, *_fixture(name)))
        print("AH-calls:", rec.calls)
    for d in diffs:
        print(d)
    print("Geen verschillen" if not diffs else f"{len(diffs)} verschil(len)")
    return 1 if diffs else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", metavar="TOKENS", type=Path,
                        help="vergelijk een verse opname met de opgeslagen fixtures")
    args = parser.parse_args()
    if args.check is not None:
        sys.exit(asyncio.run(check(args.check)))
    asyncio.run(main())
