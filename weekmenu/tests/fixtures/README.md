# Fixtures

Echte antwoorden van `api.ah.nl`, opgenomen op **2026-11-01** met `python -m tools.record_fixtures` (alleen GET en GraphQL-queries) en daarna geanonimiseerd met `python -m tools.anonymize`. Namen, adressen, postcodes, e-mail, telefoon, lid- en klantnummers en tokens zijn verwijderd, order-id's staan op `123456`, bezorg- en synctijden op een vaste datum. Productgegevens, recepten en prijzen zijn ongewijzigd. `test_fixtures_clean.py` bewaakt dit bij elke testrun.

Bestandsnaam: `<endpoint>__<parameters>.json`. Bestanden met `{status, body}` zijn antwoorden waarvan de statuscode ertoe doet; de rest is de kale body.

| Bestand | Endpoint |
|---|---|
| `bonus-metadata__v3.json` | `GET /mobile-services/bonuspage/v3/metadata` (periodes en categorieen) |
| `bonus-section__p0-<categorie>.json` | `GET /mobile-services/bonuspage/v2/section?application=AHWEBSHOP&date=<start>&promotionType=NATIONAL&category=..`, huidige periode (p0) |
| `bonus-section__p1-<categorie>.json` | idem, volgende periode (p1, al gepubliceerd bij het opnemen) |
| `graphql-bonuspromotions__p<n>-<groepsid>.json` | GraphQL `bonusPromotions` (producten van een groepsactie), 2 voor p0, 1 voor p1 |
| `graphql-recipesearch__<zoekterm>.json` | GraphQL `recipeSearch`, size 30, termen: kip, gehakt, zalm, pasta, stamppot, curry, wraps, ovenschotel |
| `graphql-recipe__<id>-s2.json` | GraphQL `recipe(id, servings: 2)`, 20 hoofdgerechten |
| `graphql-recipe__<id>-s4.json` | idem met `servings: 4`, voor `1302918` en `1302920` |
| `product-search__<ingredient>.json` | `GET /mobile-services/product/search/v2?query=..&page=0&size=20&sortOn=RELEVANCE`, voor de ingredienten van 5 recepten |
| `shoppinglist__items.json` | `GET /mobile-services/shoppinglist/v2/items`, `{status, body}` |
| `order-active__default.json` | `GET /mobile-services/order/v1/summaries/active?sortBy=DEFAULT`, `{status, body}` |
| `graphql-introspection__recipe-type.json` | introspectie op `Recipe`, `{status, body}` |

## Wat ontbreekt en waarom

- **Boodschappenlijst was leeg** (`items: []`) bij het opnemen, dus de itemstructuur van de lijst staat niet in een fixture. Er zijn bewust geen schrijfacties gedaan om dat te veranderen.
- **Er was geen actieve bestelling**: `order-active__default.json` is `{status: 404, body: {message: "Order does not exist: ..."}}`. Dat is het antwoord waar `isNoActiveOrder` in de referentie-implementatie op let. Een bestelling mét items is dus niet opgenomen.
- **Introspectie staat uit** (`INTROSPECTION_DISABLED`). De velden hieronder zijn gevonden door ze te proberen.

## Ontdekte velden

- **Receptafbeelding:** `images { url width height }` op `Recipe` (detail) en ook op de zoekresultaten van `recipeSearch`. Het is een lijst met meerdere formaten van dezelfde foto (`static.ah.nl/static/recepten/img_<n>_<breedte>x<hoogte>_JPG.jpg`, bijvoorbeeld 220x162, 302x220, 440x324, 445x297). Een enkel `image`, `imageUrl`, `photo` of `media` bestaat niet.
- **`time { cook oven wait }`** bestaat alleen op de zoekresultaten (`recipeSearch`), niet op `Recipe`. Het detail heeft alleen `cookTime` (minuten). Gebruik voor de 45-minutengrens dus `time.cook` uit de zoekresultaten.
- **`cuisines`** bestaat op `Recipe` (detail), niet op de zoekresultaten (`RecipeSummary`).
- **`courses`** komt voor in zoekresultaten en detail. Gezien in de zoekresultaten: `hoofdgerecht` (198), `lunch` (16), `bijgerecht` (8), `brunch` (8), `borrelhapje` (6), `voorgerecht` (4), `tussendoortje` (1).

## Waargenomen `cuisines`-waarden

Van de 22 opgenomen receptdetails hadden er **21 een lege lijst** en 1 had `["mexicaans"]`. Er is dus in deze steekproef maar een waarde gezien: `mexicaans`. De keukenindeling (Hollandse pot, pasta en Italiaans, Aziatisch, Mexicaans en wraps) kan daarom niet op `cuisines` alleen leunen; een mapping moet terugvallen op titel, zoekterm en ingredienten.
