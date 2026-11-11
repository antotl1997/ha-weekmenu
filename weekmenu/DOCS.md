# Weekmenu AH

## Wat het doet

Weekmenu AH maakt elke week een menu van Allerhande-recepten rond de bonus van Albert Heijn. Het zet de boodschappen voor die gerechten op je AH-lijst (Mijn lijst) of in je open bezorgbestelling, en laat het menu en de geschatte besparing zien in Home Assistant: in de app in de zijbalk, in een pushmelding en in `sensor.weekmenu`.

- Per kookmoment één gerecht. Zijn er meer eetdagen dan kookmomenten, dan worden de extra dagen restjesavonden.
- Een tabblad met de hele bonusfolder van de week, waar je met één tik een aanbieding toevoegt.
- Vaste boodschappen die je vaak koopt, voorgesteld uit je bestelgeschiedenis.
- De geschatte besparing per week, per maand en in totaal, en na de bezorging wat er echt bespaard is.
- Een kookscherm met grote hoeveelheden en stappen, en per gerecht een duim omlaag of Nooit meer.

De add-on gebruikt de onofficiële API van de Albert Heijn-app. Hij is niet verbonden aan of goedgekeurd door Albert Heijn en kan stuk gaan als AH die API aanpast.

## Installeren

Ga in Home Assistant naar Instellingen, Add-ons, Add-on Store, het menu met de drie puntjes, Repositories, en voeg toe:

    https://github.com/antotl1997/ha-weekmenu

Installeer daarna "Weekmenu AH" uit de store, start hem en zet "Toon in zijbalk" aan. De app opent dan in de zijbalk onder de naam Weekmenu.

## Inloggen bij AH

Je logt één keer in. Daarna ververst de add-on de login zelf. Inloggen kan alleen met de hand, want de AH-inlogpagina gebruikt een captcha. Bij de eerste start toont de app eerst een kort welkomstscherm.

Op een computer, in Chrome of Edge:

1. Open de link naar de AH-inlogpagina in de app.
2. Druk F12 en kies Console.
3. Log in.
4. Kopieer de regel die begint met `appie://login-exit?code=`.
5. Plak hem in de app. Dat kan ook op je telefoon, bijvoorbeeld door de regel via WhatsApp naar jezelf te sturen. Plak je de hele chatregel, dan haalt de add-on de code er zelf uit. De code is maar een paar minuten geldig.

Op de Week staat "AH-login in orde, laatst ververst <datum>". Is de login verlopen (AH weigert hem, ook bij het verversen), dan vraagt de app je opnieuw in te loggen en is de regel op de dashboardkaart rood. Dezelfde informatie staat op `sensor.weekmenu` als `login_ok` en `login_refreshed`.

Het AH-account mag gedeeld blijven: jullie AH-apps blijven gewoon werken. Raakt de add-on de login kwijt of weigert AH hem, dan krijg je een melding "AH-login verlopen" en toont de app het inlogscherm weer. Het menu van die week wordt dan pas gemaakt na opnieuw inloggen.

## Instellingen

Onder Configuratie van de add-on:

- **schedule_time (Schema-tijd)**: vanaf dit tijdstip (standaard 08:00) kijkt de add-on elk uur of de folder van de bezorgweek er is, tot 18:00 en zolang er nog geen menu is. Meestal komt de folder op vrijdag.
- **pantry (Basisvoorraad)**: producten die je altijd in huis hebt, zoals olie, zout en peper. Die komen niet op je lijst.
- **ah_app_version (AH-appversie)**: de versie van de AH-app waarmee de add-on zich bij AH meldt, standaard 9.28. Pas dit alleen aan als AH de add-on weigert met een melding over een oude app-versie; zet er dan de versie in van de AH-app op je telefoon.
- **notify_owner (Melding eigenaar)**: de notify-dienst van de telefoon die het menu, fouten, de AH-login en de bestelherinnering krijgt, zonder `notify.` ervoor (bijvoorbeeld `mobile_app_mijn_telefoon`). Je vindt de naam in Home Assistant onder Ontwikkelaarshulpmiddelen, Acties, bij `notify.`.
- **notify_today (Melding vanavond)**: een lijst notify-diensten die alleen de korte melding "Vanavond" krijgen, elke dag om 16:00. Laat de lijst leeg als niemand die hoeft.
- **dinner_time (Etenstijd)**: hoe laat het eten op tafel staat, standaard 18:30. "Vanavond" rekent de begintijd daarvan terug.

In de app, onder Instellingen:

- **Kookmomenten**: hoeveel avonden je kookt, van 1 tot het aantal eetdagen. De overige eetdagen worden restjesavonden: dan wordt voor twee avonden gekookt en niets extra gekocht.
- **Niet thuis eten**: tik de dagen aan waarop je niet thuis eet. Die avond krijgt geen gerecht en er wordt niets voor gekocht. Tik je de dag nog eens aan, dan komt het eerdere aantal personen terug.
- **Personen**: het aantal personen per dag. Dit zijn helpers in Home Assistant (`input_number.weekmenu_personen_<dag>`), die je ook daar kunt aanpassen. Per gerecht kun je het aantal personen nog wijzigen.

## Meldingen

- **Menu klaar, fouten, login en herinnering** gaan naar `notify_owner`.
- **Vanavond** gaat elke dag om 16:00 naar de diensten in `notify_today`: "Vanavond: <gerecht>, begin om <tijd>", of "Vanavond: restjes van <gerecht>". De begintijd is `dinner_time` min de kook-, oven- en wachttijd, naar beneden afgerond op een kwartier; zou dat vóór 15:00 zijn, dan noemt de melding geen tijd. Ben je die avond niet thuis, of is er geen gerecht of kookmoment, dan komt er geen melding. Hooguit één keer per dag, ook na een herstart.
- **Bestelherinnering**: op zondag tussen 17:00 en 23:00, als het menu voor de komende week klaarstaat en er nog geen bestelling is die in die week bezorgd wordt, één melding "Het menu staat klaar, maar er is nog geen bezorgmoment gekozen." Hooguit één per dag.

## Dashboardkaart

In de map `dashboard` van de repository staat `weekmenu-card.yaml`, een kaart voor een Lovelace-dashboard met Vanavond, de week per dag, de besparing van de week, de AH-login en een knop Open weekmenu. Voeg hem toe via Bewerken, Kaart toevoegen, Handmatig. Hij gebruikt Mushroom (HACS). De knop gaat naar `/e258686e_weekmenu`; heet het paneel bij jou anders (kijk in de adresbalk als je Weekmenu in de zijbalk opent), pas dan `navigation_path` aan.

## Gids

`GIDS.md` is een korte gebruikersgids voor iedereen in huis: waar je ziet wat jullie eten en hoe je iets verandert. In de app staat hij onder "Lees de gids".

## In je bestelling

De add-on schrijft alleen in een open bestelling die in de menuweek (woensdag tot en met dinsdag) bezorgd wordt, of op Mijn lijst als er geen bestelling is. Staat er op maandag of dinsdag nog een bestelling open voor deze woensdag, dan wordt het menu bewaard en komen de producten erbij zodra er een bestelling voor de menuweek is.

- De add-on rekent **nooit af**. Je kiest zelf het bezorgmoment en betaalt zelf.
- Hij haalt alleen producten weg die hij **zelf** voor een menu heeft toegevoegd en die geen gerecht meer nodig heeft, bijvoorbeeld als je een ander gerecht kiest of minder personen. Wat jullie zelf in de bestelling zetten blijft staan, ook vaste boodschappen en aanbiedingen die je zelf aantikte. Wat je zelf van de lijst haalde komt die week niet terug.
- Elke wijziging wordt eerst in een logboek genoteerd, dan geschreven, en na een crash eerst gecontroleerd. Zo komt er niets dubbel in.

## Check voor je bestelt en Klopt niet

Bovenaan de Week staat de kaart Check voor je bestelt. Daarin staat wat niet gevonden is, wat nog niet zeker in je bestelling staat, wat over is van een vorig menu, wat het menu nodig heeft maar nog niet is toegevoegd (bijvoorbeeld omdat schrijven stilstond) en welke producten een prijs per gewicht hebben (daarvan zet de add-on 1 stuk in je bestelling; pas het aantal zelf aan als dat te weinig is). Klopt alles, dan staat er "Alles staat klaar".

Pakt de add-on een verkeerd product, tik dan in het gerecht bij dat ingrediënt op Klopt niet. Je kunt een ander product zoeken en kiezen, of zeggen dat het niet hoeft of dat je het al in huis hebt. Die keuze geldt voor dat ingrediënt in elke week en wordt meteen in je bestelling of op je lijst verwerkt, op dezelfde manier als Ander gerecht. Alle keuzes staan in Instellingen onder Mijn correcties, met de naam waarvoor ze gelden en Terugdraaien. Na Terugdraaien kiest de add-on weer zelf, ook meteen in het menu van deze week. Ze staan in `overrides.json` in de datamap van de add-on en gaan dus mee in je backups.

## Koken, duim omlaag en Nooit meer

Tik in een gerecht op Koken. Je ziet de hoeveelheden uit het recept groot met het AH-product klein eronder, en de bereiding in genummerde stappen; tik op een stap als hij klaar is. Het scherm blijft aan zolang dit scherm open is, als je browser dat ondersteunt. Klaar brengt je terug naar het gerecht.

Met Duim omlaag komt een gerecht minder vaak terug: in de rangschikking telt het nog voor 75 procent. Met Nooit meer komt het niet meer in een menu, ook niet bij Ander gerecht. Het menu van deze week blijft staan; wil je het nu kwijt, tik dan op Ander gerecht. Nog een keer tikken draait de keuze terug. Instellingen heeft de lijst Nooit meer met Terugdraaien. De keuzes staan in `taste.json` in de datamap.

## Besparing nagerekend

De besparing in de app en in `sensor.weekmenu_besparing_totaal` is een schatting: wat de bonus scheelt op wat de add-on toevoegde. De dag na de bezorging leest de add-on je bezorgde bestelling bij AH en zet die ernaast. In Besparing staat dan per week "Echt bespaard (nagerekend)" met hoeveel van de toegevoegde producten echt gekocht zijn. Die regel telt niet mee in de totalen; die blijven de schatting.

- Alleen lezen: de add-on vraagt de bestelgegevens op en schrijft niets naar AH. Niet zolang schrijven stilstaat (Diagnose) of de login verlopen is; dan later.
- Hooguit één keer per dag. Is de bestelling nog niet bezorgd, is er (nog) geen bestelling te vinden of lukt het lezen niet (AH even onbereikbaar, login verlopen), dan kijkt hij de volgende dag opnieuw, tot een week na die dag. Lukt het dan nog niet, of is de bestelling geannuleerd, dan staat er "niet na te rekenen".
- Welke bestelling: de bestelling waar de add-on die week in schreef, anders elke bezorgde bestelling met een bezorgdag in de menuweek. Geen bestelling gevonden, bijvoorbeeld omdat je in de winkel kocht: na een week "niet na te rekenen".
- Wanneer: de dag na de bezorgdag die de add-on las toen hij in de bestelling schreef. Kent hij die niet (je bestelde vanaf de lijst), dan de woensdag na de menuweek. Weken van meer dan vijf weken geleden worden niet meer gelezen.
- De uitkomsten staan in `reconciliation.json` in de datamap.

Onder de weken staan twee meetgetallen over de laatste 4 weken: het aandeel ingrediëntregels zonder ingreep (geen Klopt niet voor die regel in die week, ook niet een die later is teruggedraaid) en het aandeel toegevoegde producten dat echt gekocht is. Ze staan ook in de diagnose onder `meetgetallen`.

## Backups en je AH-login

De AH-login staat in het bestand `tokens.json` in de datamap van de add-on. Die map zit in je Home Assistant-backups. Wie een backup van jou heeft, kan daarmee dus ook bij de AH-login. Bewaar backups goed en deel ze niet.

Is er een backup of de login gelekt:

1. Wijzig je AH-wachtwoord. Dat maakt de oude login ongeldig.
2. Log uit bij de add-on: in de app onder Instellingen, AH-login, Uitloggen bij AH. Dat wist `tokens.json` en ook `tokens.json.bak` (de kopie van vóór versie 0.6), en de app toont daarna het inlogscherm. Oudere backups bevatten de login nog wel; het nieuwe wachtwoord uit stap 1 maakt die ongeldig.
3. Log daarna opnieuw in, zoals hierboven.

## Uitloggen

In de app onder Instellingen staat bij AH-login de knop Uitloggen bij AH. Na bevestigen wist de add-on de AH-login. Daarna zet hij niets meer in je bestelling of op je lijst tot je opnieuw inlogt.

## Als AH iets verandert

De add-on controleert of de antwoorden van AH de vorm hebben die hij verwacht, voor de bonus, de recepten, de producten, je lijst en je bestelling. Gaat het op hetzelfde onderdeel twee keer achter elkaar mis, dan schrijft hij niets meer naar je bestelling of lijst. Je krijgt dan één melding, en de app toont bovenaan dat schrijven stilstaat. Het menu wordt wel gewoon gemaakt en bewaard.

Elk uur probeert de add-on of AH weer normaal antwoordt. Lukt dat, dan gaat schrijven vanzelf weer aan en vult de volgende ronde aan wat bleef liggen. Weigerde AH juist het schrijven zelf, dan gaat het alleen met de hand weer aan, want dat is niet te testen zonder iets te schrijven. In Instellingen, Diagnose kun je ook zelf op Opnieuw proberen tikken; tik daarna op Nu maken om aan te vullen.

Met Kopieer diagnose zet je de gegevens voor een melding op je klembord: de versies, de status per onderdeel, de laatste logregels en een samenvatting per week. Er staat geen login, code of antwoord van AH in.

De add-on heeft een watchdog: reageert hij niet meer, dan start Home Assistant hem opnieuw. Een pauze in het schrijven blijft na zo'n herstart staan.

## Problemen oplossen

- **Het inlogscherm blijft terugkomen**: de code was verlopen of niet de hele regel. Doe het inloggen opnieuw en plak de regel direct na het inloggen.
- **"AH-login verlopen"**: log opnieuw in. Het menu van die week wordt daarna alsnog gemaakt.
- **Er komt geen menu**: het menu wordt gemaakt zodra de folder van de bezorgweek er is. Kies Nu maken in de app om het met de huidige folder te maken. Controleer ook of `input_boolean.weekmenu_aan` aan staat.
- **De producten staan er niet**: zie "In je bestelling". Is er nog geen bestelling voor de menuweek, dan melden de app en de melding dat, en komen de producten erbij zodra er een is. Anders staat in de melding welke producten je zelf in de AH-app toevoegt.
- **"AH lijkt iets veranderd te hebben"** of **het werkte opeens niet meer**: AH kan zijn API aanpassen. Zie "Als AH iets verandert". Meld het via https://github.com/antotl1997/ha-weekmenu/issues met de gekopieerde diagnose; die bevat geen login. Weigert AH de add-on met een melding over een oude app-versie, zet dan `ah_app_version` op de versie van je AH-app.
