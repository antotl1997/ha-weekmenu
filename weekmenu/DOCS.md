# Weekmenu AH

## Wat het doet

Weekmenu AH maakt elke week een menu van Allerhande-recepten rond de bonus van Albert Heijn. Het zet de boodschappen voor die gerechten op je AH-lijst (Mijn lijst) of in je open bezorgbestelling, en laat het menu en de geschatte besparing zien in Home Assistant: in de app in de zijbalk, in een pushmelding en in `sensor.weekmenu`.

- Per kookmoment één gerecht. Zijn er meer eetdagen dan kookmomenten, dan worden de extra dagen restjesavonden.
- Een tabblad met de hele bonusfolder van de week, waar je met één tik een aanbieding toevoegt.
- Vaste boodschappen die je vaak koopt, voorgesteld uit je bestelgeschiedenis.
- De geschatte besparing per week, per maand en in totaal.

De add-on gebruikt de onofficiële API van de Albert Heijn-app. Hij is niet verbonden aan of goedgekeurd door Albert Heijn en kan stuk gaan als AH die API aanpast.

## Installeren

Ga in Home Assistant naar Instellingen, Add-ons, Add-on Store, het menu met de drie puntjes, Repositories, en voeg toe:

    https://github.com/antotl1997/ha-weekmenu

Installeer daarna "Weekmenu AH" uit de store, start hem en zet "Toon in zijbalk" aan. De app opent dan in de zijbalk onder de naam Weekmenu.

## Inloggen bij AH

Je logt één keer in. Daarna ververst de add-on de login zelf. Inloggen kan alleen met de hand, want de AH-inlogpagina gebruikt een captcha.

1. Open de app in de zijbalk. Zonder login toont hij het inlogscherm met een knop naar de AH-inlogpagina.
2. Doe dit op een computer, in Chrome of Edge. Open de AH-inlogpagina, druk op F12 en kies het tabblad Console.
3. Log in bij AH. In de console verschijnt een regel met `appie://login-exit?code=...`.
4. Kopieer die hele regel, plak hem in het vak in de app en kies Inloggen. De code is maar kort geldig, dus doe stap 3 en 4 meteen na elkaar.

Op een telefoon kan dit niet, omdat je daar geen ontwikkelaarsconsole hebt om die regel uit te lezen. Gebruik een computer.

Het AH-account mag gedeeld blijven: jullie AH-apps blijven gewoon werken. Raakt de add-on de login kwijt of weigert AH hem, dan krijg je een melding "AH-login verlopen" en toont de app het inlogscherm weer. Het menu van die week wordt dan pas gemaakt na opnieuw inloggen.

## Instellingen

Onder Configuratie van de add-on:

- **schedule_time (Schema-tijd)**: vanaf dit tijdstip (standaard 08:00) kijkt de add-on elk uur of de folder van de bezorgweek er is, tot 18:00 en zolang er nog geen menu is. Meestal komt de folder op vrijdag.
- **pantry (Basisvoorraad)**: producten die je altijd in huis hebt, zoals olie, zout en peper. Die komen niet op je lijst.

In de app, onder Instellingen:

- **Kookmomenten**: hoeveel avonden je kookt, van 1 tot het aantal eetdagen. De overige eetdagen worden restjesavonden: dan wordt voor twee avonden gekookt en niets extra gekocht.
- **Niet thuis eten**: tik de dagen aan waarop je niet thuis eet. Die avond krijgt geen gerecht en er wordt niets voor gekocht. Tik je de dag nog eens aan, dan komt het eerdere aantal personen terug.
- **Personen**: het aantal personen per dag. Dit zijn helpers in Home Assistant (`input_number.weekmenu_personen_<dag>`), die je ook daar kunt aanpassen. Per gerecht kun je het aantal personen nog wijzigen.

## In je bestelling

De add-on schrijft alleen in een open bestelling die in de menuweek (woensdag tot en met dinsdag) bezorgd wordt, of op Mijn lijst als er geen bestelling is. Staat er op maandag of dinsdag nog een bestelling open voor deze woensdag, dan wordt het menu bewaard en komen de producten erbij zodra er een bestelling voor de menuweek is.

- De add-on rekent **nooit af**. Je kiest zelf het bezorgmoment en betaalt zelf.
- Hij haalt alleen producten weg die hij **zelf** voor een menu heeft toegevoegd en die geen gerecht meer nodig heeft, bijvoorbeeld als je een ander gerecht kiest of minder personen. Wat jullie zelf in de bestelling zetten blijft staan, ook vaste boodschappen en aanbiedingen die je zelf aantikte. Wat je zelf van de lijst haalde komt die week niet terug.
- Elke wijziging wordt eerst in een logboek genoteerd, dan geschreven, en na een crash eerst gecontroleerd. Zo komt er niets dubbel in.

## Backups en je AH-login

De AH-login staat in het bestand `tokens.json` in de datamap van de add-on. Die map zit in je Home Assistant-backups. Wie een backup van jou heeft, kan daarmee dus ook bij de AH-login. Bewaar backups goed en deel ze niet.

Is er een backup of de login gelekt:

1. Wijzig je AH-wachtwoord. Dat maakt de oude login ongeldig.
2. Verwijder `tokens.json` bij de add-on, via uitloggen in de app. De uitlogknop komt in versie 0.7. Tot die tijd kun je de add-on verwijderen en opnieuw installeren (dat wist zijn data), of het bestand in de datamap van de add-on weghalen.
3. Log daarna opnieuw in, zoals hierboven.

## Problemen oplossen

- **Het inlogscherm blijft terugkomen**: de code was verlopen of niet de hele regel. Doe het inloggen opnieuw en plak de regel direct na het inloggen.
- **"AH-login verlopen"**: log opnieuw in. Het menu van die week wordt daarna alsnog gemaakt.
- **Er komt geen menu**: het menu wordt gemaakt zodra de folder van de bezorgweek er is. Kies Nu maken in de app om het met de huidige folder te maken. Controleer ook of `input_boolean.weekmenu_aan` aan staat.
- **De producten staan er niet**: zie "In je bestelling". Is er nog geen bestelling voor de menuweek, dan melden de app en de melding dat, en komen de producten erbij zodra er een is. Anders staat in de melding welke producten je zelf in de AH-app toevoegt.
- **Het werkte opeens niet meer**: AH kan zijn API aanpassen. Kijk in het logboek van de add-on en meld het via https://github.com/antotl1997/ha-weekmenu/issues, zonder tokens of persoonsgegevens.
