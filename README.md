# Weekmenu AH

Home Assistant add-on that builds a weekly dinner menu from Allerhande recipes around the current Albert Heijn bonus, puts the groceries on your AH list or in your open delivery order, and shows the menu and the estimated savings in Home Assistant.

This uses the unofficial API of the Albert Heijn app. It is not affiliated with or endorsed by Albert Heijn, and it can break whenever AH changes that API. It never checks out, never removes anything from your list or order, and keeps your AH login only inside the add-on.

## Install

In Home Assistant: Settings, Add-ons, Add-on Store, the three-dot menu, Repositories, and add:

    https://github.com/antotl1997/ha-weekmenu

Then install "Weekmenu AH" from the store.

## When it runs

The menu is made as soon as the AH bonus folder of the delivery week is out, usually on the Friday before. From `schedule_time` (default 08:00) until 18:00 the add-on checks once an hour, as long as there is no menu for that week yet. If the folder is still not out on Sunday at 18:00, it uses the current folder and says so in the message. You get one push message, when the menu is made.

## Not eating at home

In the app, under Settings, tap the days (Wednesday to Tuesday) you do not eat at home. A tapped day sets the persons of that weekday to 0: no dish, nothing bought, and the next day moves up (never past Tuesday). Tap the day again and the earlier number of persons comes back (2 if there was none); the add-on keeps that number in `/data/niet_thuis.json`, also after a restart. If you set a persons helper to 0 in Home Assistant yourself, the app shows that day as tapped. The week tab, the push message and `sensor.weekmenu` call such an evening "niet thuis".
