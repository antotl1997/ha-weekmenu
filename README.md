# Weekmenu AH

Home Assistant add-on that builds a weekly dinner menu from Allerhande recipes around the current Albert Heijn bonus, puts the groceries on your AH list or in your open delivery order, and shows the menu and the estimated savings in Home Assistant.

This uses the unofficial API of the Albert Heijn app. It is not affiliated with or endorsed by Albert Heijn, and it can break whenever AH changes that API. It never checks out, never removes anything from your list or order, and keeps your AH login only inside the add-on.

## Install

In Home Assistant: Settings, Add-ons, Add-on Store, the three-dot menu, Repositories, and add:

    https://github.com/antotl1997/ha-weekmenu

Then install "Weekmenu AH" from the store.

## When it runs

The menu is made as soon as the AH bonus folder of the delivery week is out, usually on the Friday before. From `schedule_time` (default 08:00) until 18:00 the add-on checks once an hour, as long as there is no menu for that week yet. If the folder is still not out on Sunday at 18:00, it uses the current folder and says so in the message. You get one push message, when the menu is made.

Nu maken (in the app or through `hassio.addon_stdin`) does not wait: before the folder is out it makes the menu with the current folder, with the same warning. When the folder of the delivery week then comes out, the hourly check sends one push message "De nieuwe folder is er" and sets `new_folder_available: true` on `sensor.weekmenu` and in the app. It never changes the menu by itself. The app shows "Nieuwe folder is er" at the top of the Week tab with the button Menu bijwerken, which makes a new menu from the new folder for the same evenings, persons (also those changed per dish) and leftover nights, keeps removed dishes removed, and takes off what only the old menu needed (`POST api/run` with `{"mode": "bijwerken"}`). The check runs up to and including the Sunday before delivery. A folder that only comes out on the Monday or Tuesday is not signalled: by then the add-on works on the week after, and a flag still standing is cleared.

## Not eating at home

In the app, under Settings, tap the days (Wednesday to Tuesday) you do not eat at home. A tapped day sets the persons of that weekday to 0: no dish and nothing bought. Tap the day again and the earlier number of persons comes back (2 if there was none); the add-on keeps that number in `/data/niet_thuis.json`, also after a restart. If you set a persons helper to 0 in Home Assistant yourself, the app shows that day as tapped. The week tab, the push message and `sensor.weekmenu` call such an evening "niet thuis".

## Cooking moments and leftovers

The days you do eat at home are the eat days. Under Settings you choose the number of cooking moments (`input_number.weekmenu_avonden`), from 1 up to the number of eat days; a value outside that range is bounded, not refused. With fewer cooking moments than eat days, the extra eat days become leftover nights: the earliest cooking days cook for their own evening plus the next eat day (2 + 2 = 4 persons), at most one leftover night each, and never with fish. Nothing extra is bought for a leftover night. The app, the push message and `sensor.weekmenu` show it as "<day> · restjes van <dish>"; on the sensor it is an entry of its own with the field `restjes_van`. Swapping the dish keeps the leftover night, removing the dish drops it too. With fewer than half as many cooking moments as eat days, the last eat days get no dish; those, and a cooking day the planner could not fill, show as "<day> · geen kookmoment". When days are set to "niet thuis", the add-on lowers the helper to the number of eat days if needed. A dish with a leftover night can be set to up to 16 persons.
