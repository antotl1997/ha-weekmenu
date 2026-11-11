# Changelog

## 0.8.0

- **Vanavond.** Elke dag om 16:00 een korte melding: "Vanavond: <gerecht>, begin om <tijd>", of "Vanavond: restjes van <gerecht>". De begintijd is de etenstijd (`dinner_time`, standaard 18:30) min kook-, oven- en wachttijd, naar beneden afgerond op een kwartier; zou dat vóór 15:00 zijn, dan staat er geen begintijd. Niet thuis, geen gerecht of geen kookmoment: geen melding. Hooguit één keer per dag, ook na een herstart. Hetzelfde staat bovenaan de Week in de app, als attribuut `vanavond` op `sensor.weekmenu` en in `api/week`.
- **Meldingsdoelen als instelling.** `notify_owner` krijgt het menu, fouten, de login en de herinnering; `notify_today` (een lijst) krijgt alleen Vanavond. De vaste naam in de code is weg.
- **Bestelherinnering.** Op zondag tussen 17:00 en 23:00 (dus ook na een menu dat om 18:00 gemaakt wordt), als het menu voor de komende week klaarstaat maar er nog geen bestelling is die in die week bezorgd wordt, één melding: "Het menu staat klaar, maar er is nog geen bezorgmoment gekozen." De add-on leest daarvoor alleen de bestelling. Is de bezorgdag van een open bestelling niet te lezen, dan komt er geen herinnering.
- **Inloggen zonder stress.** Het inlogscherm is een stappenplan; de uitleg over F12 staat onder "Hoe werkt dit?". De code plakken kan ook op je telefoon, ook als een hele chatregel. Op de Week staat "AH-login in orde, laatst ververst <datum>"; is de login verlopen, dan vraagt de app om opnieuw in te loggen en is de regel op de dashboardkaart rood. `sensor.weekmenu` heeft de attributen `login_ok` en `login_refreshed`.
- **Welkom en een gids.** Bij de eerste start, zonder login en zonder menu, een kort welkomstscherm met drie stappen. `GIDS.md` is een gebruikersgids van één pagina, in de app te openen via "Lees de gids".
- **Gewone taal.** "Er loopt al een ronde" heet nu "Het menu wordt nu gemaakt, even geduld" en "Onzeker of gelukt" heet "Nog niet zeker of dit in je bestelling staat" (of "op je lijst"). Grijze tekst heeft minstens 4.5:1 contrast en alle knoppen zijn minstens 44 px.
- **Dashboardkaart.** `dashboard/weekmenu-card.yaml`: Vanavond, de week per dag (restjes, niet thuis en geen gerecht als rustige regels), de besparing van de week, de AH-login en een knop Open weekmenu. Zonder `condition: template`.
- `sensor.weekmenu` wordt nu één keer per dag opnieuw gezet (en als de login verandert), zodat `vanavond` meeloopt. Het menu zelf verandert daardoor niet.
- Een verzoek dat niet via Home Assistant komt, wordt geweigerd en nu ook gelogd, met alleen het adres en het pad.
- Zet je terug naar 0.7, dan vergeet die versie dat Vanavond of de herinnering vandaag al verstuurd is zodra hij `state.json` bijwerkt; op die dag kan er dan één keer een tweede komen.

## 0.7.0

- **Als AH iets verandert, weet je het meteen.** De add-on controleert of de antwoorden van AH (bonus, recepten, producten, lijst en bestelling) de vorm hebben die hij verwacht. Gaat het twee keer achter elkaar mis op hetzelfde onderdeel, dan schrijft hij niets meer naar je bestelling of lijst en krijg je één melding: "AH lijkt iets veranderd te hebben, schrijven naar je bestelling staat stil. Kijk in de Weekmenu-app bij Diagnose." Het menu wordt wel bewaard. Zodra AH weer normaal antwoordt gaat het schrijven vanzelf weer aan en vult het elk uur aan wat bleef liggen, of je tikt in Diagnose op Opnieuw proberen.
- **Een blokkade van AH (HTTP 403) stopt de ronde meteen**, net als te veel verzoeken (429), in plaats van door te gaan met de volgende categorie.
- **Diagnose in Instellingen.** Zie of AH antwoordt zoals verwacht, wanneer de laatste geslaagde AH-sync was en met welke app-versie de add-on zich meldt. "Kopieer diagnose" zet de versie, de status per onderdeel, de laatste 50 logregels, een samenvatting per week en de duur van de laatste menuberekening op je klembord, zonder login, codes of antwoorden van AH. Handig bij een melding op GitHub.
- **Uitloggen bij AH.** Een knop in Instellingen wist de AH-login (ook de oude kopie `tokens.json.bak`). Daarna toont de app het inlogscherm.
- **AH-appversie als instelling** (`ah_app_version`, standaard 9.28). Weigert AH een oude app-versie, dan pas je dit aan zonder op een nieuwe versie van de add-on te wachten.
- **Watchdog.** Home Assistant start de add-on opnieuw als hij niet meer reageert.
- De melding "werk de add-on bij" komt nu echt één keer, ook na een herstart. Staat de AH-login in een bestand van een nieuwere versie, dan toont de app die melding in plaats van het inlogscherm.
- Voor ontwikkelaars: `python -m tools.record_fixtures --check <tokens.json>` vergelijkt een verse, alleen-lezen opname met de opgeslagen voorbeeldantwoorden en noemt de verschillen. Gebruik daarvoor een eigen login, niet een kopie van die van de add-on; de check ververst nooit.
- Zet je terug naar 0.6, dan vergeet die versie een lopende pauze van het schrijven (en de melding daarover) zodra hij `state.json` bijwerkt.

## 0.6.0

- **Geen half bestand meer na een stroomstoring.** Elk bestand dat de add-on bijhoudt (de weeklogboeken, `totaal.json`, `niet_thuis.json`, `state.json` en de AH-login in `tokens.json`) wordt eerst volledig naar een tijdelijk bestand geschreven en pas daarna verwisseld. Valt de stroom halverwege weg, dan staat de vorige versie er nog heel.
- **Versienummer in de bestanden.** Elk van die bestanden krijgt een schemaversie. Zet je een oudere add-on terug op gegevens van een nieuwere, dan schrijft hij niets in die bestanden en niets in je AH-lijst of bestelling voor die week, en krijg je één melding: "Deze versie van de add-on is ouder dan je gegevens; werk de add-on bij". De eerste keer dat een bestand uit een eerdere versie wordt bijgewerkt, blijft de oude versie één keer bewaard als `<naam>.bak` ernaast.
- **De AH-login raakt minder snel kwijt.** Een beschadigd of onleesbaar `tokens.json` geeft nu "opnieuw inloggen" in plaats van een fout, en het tijdelijke bestand van de login is vanaf het begin alleen voor de add-on leesbaar.
- **De app blijft bereikbaar terwijl het menu wordt gekozen.** Het rekenwerk van de planner draait naast de rest, en hoe lang het duurde staat in het logboek en als `planner_seconds` in de status.

## 0.5.0

- **Winkelklaar.** Er is nu een volledige handleiding (`DOCS.md`, zichtbaar in het tabblad Documentatie van de add-on), een icoon en logo, en Nederlandse en Engelse namen en uitleg bij de instellingen. De handleiding legt ook uit dat de AH-login in je Home Assistant-backups zit en wat je doet bij een lek.
- **Vaste versies.** De add-on installeert exact dezelfde versies als waarmee getest is (`requirements.txt`), en een versie-test houdt `config.yaml` en `pyproject.toml` gelijk.
- **Automatische controle.** Elke push draait de tests, de Chrome-test van de app, een build voor amd64 en aarch64 en een audit van de dependencies op bekende kwetsbaarheden. Dependabot houdt de versies bij.
- **Licentie.** Apache-2.0 (`LICENSE`).
- Geen wijzigingen aan het gedrag van de add-on.

## 0.4.0

- **Opschonen.** Bij Ander gerecht, Nieuw menu, Menu bijwerken, minder personen en het nieuwe Gerecht weghalen haalt de add-on producten van de lijst of uit de bestelling die hij zelf voor dat menu heeft toegevoegd en die geen gerecht meer nodig heeft. Wat jullie zelf op de lijst of in de bestelling zetten blijft staan, ook vaste boodschappen en losse aanbiedingen die je zelf aantikte. Elke verlaging loopt via hetzelfde write-ahead-logboek als toevoegen, en afrekenen doet de add-on nog steeds nooit. De regel in de README dat de add-on nooit iets verwijdert is hiermee vervallen.
- **Kookmomenten en restjes.** In plaats van "kookavonden" kies je kookmomenten, van 1 tot het aantal eetdagen. Zijn er meer eetdagen dan kookmomenten, dan worden de extra eetdagen restjesavonden ("<Dag> · restjes van <gerecht>"). Let op, dat is een ander gedrag dan voorheen: met 7 eetdagen en 5 kookmomenten zijn er nu 2 restjesavonden. De kookdag kookt voor zijn eigen avond plus de restjesavond (2 + 2 = 4 personen), nooit met vis, en er wordt niets extra voor gekocht. Een gerecht met restjes kan op tot 16 personen.
- **Menu vóór de nieuwe folder.** Nu maken maakt altijd een menu, ook als de folder van de bezorgweek er nog niet is (met de huidige folder en een waarschuwing). Komt de nieuwe folder later, dan krijg je één melding "De nieuwe folder is er" en toont de app bovenaan "Nieuwe folder is er" met de knop Menu bijwerken. Die maakt een nieuw menu op de nieuwe folder met dezelfde eetdagen, personen, restjes en weggehaalde dagen. De automatische ronde past een bestaand menu nooit zelf aan.
- **Tabblad Aanbiedingen.** De hele bonusfolder van de menuweek per categorie, met een zoekveld. Een plusknop zet het aantal dat de actie vraagt (bijvoorbeeld 2 bij 1+1 gratis) in de bestelling of op de lijst. Het telt mee in de besparing, op een eigen regel "waarvan losse aanbiedingen", en in het totaal. Een losse aanbieding wordt nooit opgeschoond.
- **Alleen de bestelling van de menuweek.** De add-on schrijft alleen nog in een open bestelling die in de menuweek (woensdag tot en met dinsdag) bezorgd wordt. Staat er op maandag of dinsdag nog een bestelling open voor deze woensdag, of is de bezorgdag niet te lezen, dan wordt het menu bewaard en niets toegevoegd of verlaagd, ook geen vaste boodschappen of aanbiedingen. De melding en de app zeggen dat, en de uurcheck voegt alles toe zodra er een bestelling of lijst voor de menuweek is.
- Wat je zelf uit de bestelling of van de lijst haalde, komt die week niet terug via Terug, Ander gerecht, Nieuw menu of Menu bijwerken. De meldingregel over wat er over is heet nu "Over van het vorige menu".
- Instellingen: "Niet thuis eten" en "Kookmomenten" tonen hoeveel eetdagen, kookmomenten en restjesavonden dat geeft.

## 0.3.0

- Niet thuis eten: tik in Instellingen de dagen aan waarop jullie niet thuis eten. Die avond krijgt geen gerecht en er wordt niets voor gekocht. De add-on onthoudt het aantal personen van die dag. Dit vervangt de oude ouders-helper.

## 0.2.1

- Avonden met 0 personen tellen als niet thuis in de app, de melding en `sensor.weekmenu`. De personen-helpers gaan van 0 tot 8.

## 0.2.0

- Het menu wordt gemaakt zodra de folder van de bezorgweek uit is, meestal op vrijdag. Vanaf `schedule_time` tot 18:00 kijkt de add-on elk uur. Is de folder zondag om 18:00 nog niet uit, dan wordt de huidige folder gebruikt en staat dat in de melding.
- De app en de melding noemen voor welke bestelling het menu is ("Voor de bestelling van woensdag ...").
- Een onafgemaakte ronde wordt één keer aangevuld, en een verlopen login verbruikt die kans niet.

## 0.1.0

- Eerste versie. Een weekmenu van Allerhande-recepten rond de AH-bonus, met de boodschappen in je open bestelling of op Mijn lijst, een logboek per menuweek zodat niets dubbel wordt toegevoegd, en een melding met de besparing.
- App via ingress met de weekkaart, het gerechtscherm (ander gerecht, terug, personen), de besparing en Instellingen.
- Vaste boodschappen in de bonus: producten die jullie vaak kopen en nu in de aanbieding zijn, als voorstel dat je met een tik toevoegt.
- Sensoren `sensor.weekmenu` en de totale besparing in Home Assistant.
