"""Record raw AH responses outside the repo: python -m tools.record_fixtures

Read-only: GET requests and GraphQL queries only. Writes to
%APPDATA%\\weekmenu-test\\raw\\. Request headers are never written.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from app.ahclient import BASE_URL, AhClient
from app.tokens import TokenStore

ROOT = Path(os.environ["APPDATA"]) / "weekmenu-test"
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
    def __init__(self, client: AhClient):
        self.c = client
        self.calls = 0
        self.log: list[str] = []

    async def fetch(self, method: str, path: str, json_body=None):
        """Returns (status, parsed body or text). Never raises on HTTP status."""
        await asyncio.sleep(DELAY)
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


if __name__ == "__main__":
    asyncio.run(main())
