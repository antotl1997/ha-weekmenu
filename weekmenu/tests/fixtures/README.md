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

## Aankoopgeschiedenis (`history-*`)

Opgenomen op **2026-11-01** met `python -m tools.record_history` (18 calls, alleen GraphQL-queries en GET, geen mutation) en geanonimiseerd met `python -m tools.anonymize history`. Bron voor de endpoints: albert-heijn-mcp `src/ahapi/orders.ts`, `receipts.ts` en `bonus.ts`.

| Bestand | Endpoint |
|---|---|
| `history-graphql-fulfillments__closed.json` | GraphQL `orderFulfillments(status: CLOSED)`, de 10 laatste bestellingen, `{status, body}` |
| `history-order-details__n0..n4.json` | `GET /mobile-services/order/v1/<id>/details-grouped-by-taxonomy` voor de 5 nieuwste (n3 is geannuleerd en leeg), `{status, body}` |
| `history-graphql-receipts__limit50.json` | GraphQL `posReceiptsPage(limit 50)`, kassabonnen nieuwste eerst, `{status, body}` |
| `history-graphql-receipt__n0..n4.json` | GraphQL `posReceiptDetails` zoals albert-heijn-mcp hem vraagt (zonder product-id), `{status, body}` |
| `history-graphql-receipt-ids__n0.json`, `__n3.json` | idem met het veld `id` op de productregel erbij |
| `history-graphql-receipt-probe__fields.json` | proef naar een product-id op `PosReceiptProduct`: `400` met de velden die niet bestaan |
| `history-bonus-metadata__v3.json` | `GET /mobile-services/bonuspage/v3/metadata` op dezelfde dag |
| `history-previously-bought__p0.json`, `__p1.json` | `GET /mobile-services/bonuspage/v2/section/previously-bought?application=AHWEBSHOP&date=<start>` voor de huidige en volgende periode, `{status, body}` |

Ids zijn vervangen door vaste nep-ids die tussen de bestanden kloppen: bestellingen `900001` en verder (in de volgorde van de lijst, dus `n0` = `900001`), kassabonnen `receipt-1` en verder. Weggehaald: bezorgadres (`address`), factuur (`invoiceId`), betalingen (`orderPayments`, `payments`), tijdvak (`deliveryTimePeriod`, `closingTime`, `timeDisplay`), reden van annuleren, en de status van een bestelling behalve "Geannuleerd" (die kan een betaalstatus noemen). Van een kassabon blijft de datum, de tijd staat op 12:00. Producten uit de categorie gezondheid (medicijnen) zijn uit de bestellingen gehaald. Productids, titels, aantallen, prijzen en datums zijn ongewijzigd.

### Ontdekt

- **Een kassabon kent geen webshop-id.** `PosReceiptProduct` heeft `id`, `name` (afgekort tot 12 tekens, bijvoorbeeld `AH SPERZIEBN`), `quantity`, `price`, `amount`. Dat `id` is het **hq-id** (AH-artikelnummer): het komt overeen met `hqId` op bestelde producten en op de kaartjes van "Eerder gekocht" (`CHEDDAR RASP` = 600439 = AH Cheddar geraspte kaas). `productId`, `webshopId`, `wsId`, `hqId`, `gtin`, `ean`, `barcode`, `articleNumber`, `sku` en `product` bestaan niet.
- **"Eerder gekocht"** (`sectionType: PBO`) geeft alleen losse productkaartjes met `webshopId`, `hqId` (soms `0`: onbekend), `mainCategory`, `bonusMechanism` in wisselende hoofdletters (`2 VOOR 5.00`, `1 + 1 GRATIS`, `2e Halve Prijs`) en vaak geen `currentPrice`.
- **Een bestelling** heeft `orderState` (`DELIVERED`, `CANCELLED`) en per product `quantity` en `allocatedQuantity`.
- Hoofdcategorieen die voorkomen: `Vlees`, `Vleeswaren`, `Vis`, `Groente, aardappelen`, `Maaltijden, salades`, `Kaas`, `Zuivel, eieren`, `Frisdrank, sappen, water`, `Borrel, chips, snacks`, `Huishouden`, `Drogisterij`, `Bakkerij`, `Pasta, rijst, wereldkeuken`.
