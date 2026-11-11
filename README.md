# Weekmenu AH

Home Assistant add-on that builds a weekly dinner menu from Allerhande recipes around the current Albert Heijn bonus, puts the groceries on your AH list or in your open delivery order, and shows the menu and the estimated savings in Home Assistant.

This uses the unofficial API of the Albert Heijn app. It is not affiliated with or endorsed by Albert Heijn, and it can break whenever AH changes that API. It never checks out. The only things it ever removes from your list or order are products it added itself for a menu and no dish needs any more (see Cleaning up); what you put there yourself stays. It keeps your AH login only inside the add-on.

## What it does

- Makes a menu of Allerhande dishes around the bonus, one dish per cooking moment, and puts the groceries on your AH list or in your open delivery order. Nothing is ever checked out.
- With more eat days than cooking moments, the extra eat days become leftover nights ("restjes van <dish>"): the dish is cooked for both evenings and nothing extra is bought.
- Cleans up after itself: when you swap a dish, remove one, lower the persons or make a new menu, it takes the products it added itself and no dish needs any more off the list or out of the order.
- Makes the menu early: Nu maken works before the new folder is out, and when the folder then appears the app offers Menu bijwerken ("Nieuwe folder is er").
- Shows the whole bonus folder of the week in the Aanbiedingen tab, per category with search, and adds an offer with one tap.
- Shows the estimated savings per week, per month and in total in the app and in Home Assistant.

## Install

In Home Assistant: Settings, Add-ons, Add-on Store, the three-dot menu, Repositories, and add:

    https://github.com/antotl1997/ha-weekmenu

Then install "Weekmenu AH" from the store.

## When it runs

The menu is made as soon as the AH bonus folder of the delivery week is out, usually on the Friday before. From `schedule_time` (default 08:00) until 18:00 the add-on checks once an hour, as long as there is no menu for that week yet. If the folder is still not out on Sunday at 18:00, it uses the current folder and says so in the message. You get one push message, when the menu is made. A Dutch one-page user guide for everyone in the house is in [`weekmenu/GIDS.md`](weekmenu/GIDS.md) and in the app under "Lees de gids".

## Tonight, the reminder and the dashboard card

Every day at 16:00 the targets in `notify_today` get one short push, "Vanavond: <dish>, begin om <time>" (the start is `dinner_time` minus cooking, oven and waiting time, rounded down to a quarter) or "Vanavond: restjes van <dish>"; nothing when nobody eats at home, there is no dish or no cooking moment. The same line is the `vanavond` attribute of `sensor.weekmenu`. All other pushes go to `notify_owner`, among them one reminder on Sunday from 17:00 when the menu for the coming week is ready but no order is delivered in that week yet. Both are sent at most once a day, also over a restart. `sensor.weekmenu` also carries `login_ok` and `login_refreshed`; the app shows a red card when the AH login expired or was not refreshed for 7 days. [`weekmenu/dashboard/weekmenu-card.yaml`](weekmenu/dashboard/weekmenu-card.yaml) is a Lovelace card (Mushroom and markdown, no template conditions) with tonight, the week, the savings, the login and an "Open weekmenu" button.

Nu maken (in the app or through `hassio.addon_stdin`) does not wait: before the folder is out it makes the menu with the current folder, with the same warning. When the folder of the delivery week then comes out, the hourly check sends one push message "De nieuwe folder is er" and sets `new_folder_available: true` on `sensor.weekmenu` and in the app. It never changes the menu by itself. The app shows "Nieuwe folder is er" at the top of the Week tab with the button Menu bijwerken, which makes a new menu from the new folder for the same evenings, persons (also those changed per dish) and leftover nights, keeps removed dishes removed, and takes off what only the old menu needed (`POST api/run` with `{"mode": "bijwerken"}`). The check runs up to and including the Sunday before delivery. A folder that only comes out on the Monday or Tuesday is not signalled: by then the add-on works on the week after, and a flag still standing is cleared.

## Not eating at home

In the app, under Settings, tap the days (Wednesday to Tuesday) you do not eat at home. A tapped day sets the persons of that weekday to 0: no dish and nothing bought. Tap the day again and the earlier number of persons comes back (2 if there was none); the add-on keeps that number in `/data/niet_thuis.json`, also after a restart. If you set a persons helper to 0 in Home Assistant yourself, the app shows that day as tapped. The week tab, the push message and `sensor.weekmenu` call such an evening "niet thuis".

## Cooking moments and leftovers

The days you do eat at home are the eat days. Under Settings you choose the number of cooking moments (`input_number.weekmenu_avonden`), from 1 up to the number of eat days; a value outside that range is bounded, not refused. With fewer cooking moments than eat days, the extra eat days become leftover nights: the earliest cooking days cook for their own evening plus the next eat day (2 + 2 = 4 persons), at most one leftover night each, and never with fish. Nothing extra is bought for a leftover night. The app, the push message and `sensor.weekmenu` show it as "<day> · restjes van <dish>"; on the sensor it is an entry of its own with the field `restjes_van`. Swapping the dish keeps the leftover night, removing the dish drops it too. With fewer than half as many cooking moments as eat days, the last eat days get no dish; those, and a cooking day the planner could not fill, show as "<day> · geen kookmoment". When days are set to "niet thuis", the add-on lowers the helper to the number of eat days if needed. A dish with a leftover night can be set to up to 16 persons.

## Cleaning up

When you pick Ander gerecht, Nieuw menu, Menu bijwerken, fewer persons or Gerecht weghalen (a new button on the dish screen: the evening gets no dish, and its leftover night goes too), the add-on lowers what it added itself and the new menu no longer needs. The new quantity is the current quantity minus what the add-on added and is no longer needed, never lower than what is on there besides the add-on. Products you ticked off on the list are left alone, and so are Vaste boodschappen and Aanbiedingen you tapped yourself: those are never cleaned up. Every lowering goes through the same write-ahead log as adding: noted first, then written, and after a crash first checked whether it arrived, never blindly repeated. A menu that is not for a coming week any more (the week has started) is not cleaned up. Whatever a lowering finds you already took off yourself is remembered, and that week no later step (Terug, Ander gerecht, Nieuw menu, Menu bijwerken) adds it back. The app tells you how many products went ("2 producten uit je bestelling gehaald"). The add-on never checks out.

## Which order

The add-on only writes into an open order whose delivery day lies in the menu week (Wednesday to Tuesday); it reads that day from the order summary or from the open orders. On a Monday or Tuesday the open order is usually still the one for this Wednesday, while the menu is for the week after. Then nothing is added or lowered, also no Vaste boodschappen or Aanbiedingen: the menu is saved, and the app and the message say "Het menu staat klaar; de producten worden toegevoegd zodra er een bestelling of lijst voor <bezorgdatum> is". The hourly check (with the weekmenu on) adds the products as soon as there is an order for the menu week, or no order and so the list. The same happens when the delivery day cannot be read. Only the week the schedule works on is retried. For any other week (a change to the running week while next week's order is open, or a held menu once the schedule has moved on to the week after) and for a tapped staple or offer, the note instead names the products to add yourself in the AH app ("Deze producten konden niet in je bestelling. Voeg ze zelf toe in de AH-app: ..."), and a held menu the schedule moved past gets that as one push.

## Offers

The Aanbiedingen tab (between Week and Besparing) lists the whole bonus folder of the menu week, per category, with a search field on the title. Each product shows its photo, the action, the price (the old price struck through) and a plus button. A tap puts the quantity the action asks for (one piece, or 2 for "1+1 gratis") in the open order, or on the list when there is none, through the same write route as Vaste boodschappen: one lock, written to the log first, never retried. The button then becomes a check with the quantity, and tapping twice adds nothing twice. An offer you tapped is a loose offer: it has its own line "waarvan losse aanbiedingen" under the savings of the week (next to "waarvan vaste boodschappen"), counts in the total, and is never cleaned up. Only this week and the next menu week can be added to (`GET api/offers?q=&category=`, `POST api/offers/<product id>`).

## License

Apache License 2.0, see [LICENSE](LICENSE). Credits for the parts derived from albert-heijn-mcp are in `weekmenu/NOTICE`.
