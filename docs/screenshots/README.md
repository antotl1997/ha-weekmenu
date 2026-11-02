# Screenshots

Mockups with example data, made before the app was built. They show the intended direction, not the final app.

| | |
|---|---|
| ![Dashboard card](1-dashboard.png) | ![Week](2-week.png) |
| ![Dish](3-gerecht.png) | ![Savings and settings](4-besparing.png) |

1. Card on the Home Assistant dashboard
2. The week in the Weekmenu app
3. One dish, with swap and preparation
4. Savings and settings

## De echte app (testdata)

Screenshots of the built Weekmenu app, made by `weekmenu/tests/test_frontend.py` in headless Chrome at phone size (390 × 844). The data is test data: no real menu, account or order. Without Allerhande photos each dish shows its colour block.

| | |
|---|---|
| ![Week](app-week.png) | ![Dish](app-gerecht.png) |
| ![Savings](app-besparing.png) | ![Settings](app-instellingen.png) |
| ![Login](app-login.png) | |

1. Week: under the title the order it is for ("Voor de bestelling van woensdag 4 november"), then the savings of the week, then one card per evening, the parents' evening as a quiet card, and under the dishes "Ook in de bonus": staples bought often that are on offer, each with a plus button
2. One dish: persons, ingredients with the AH product and the bonus offer, preparation and swap
3. Savings: this month, since the start, and per week
4. Settings: the same helpers as in Home Assistant, plus Nieuw menu and Nu maken
5. Login: paste the `appie://login-exit?code=…` line from the browser console
