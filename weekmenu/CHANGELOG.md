# Changelog

## 0.4.0

- **Opschonen.** Bij Ander gerecht, Nieuw menu, Menu bijwerken, minder personen en het nieuwe Gerecht weghalen haalt de add-on producten van de lijst of uit de bestelling die hij zelf voor dat menu heeft toegevoegd en die geen gerecht meer nodig heeft. Wat jullie zelf op de lijst of in de bestelling zetten blijft staan, ook vaste boodschappen en losse aanbiedingen die je zelf aantikte. Elke verlaging loopt via hetzelfde write-ahead-logboek als toevoegen, en afrekenen doet de add-on nog steeds nooit. De regel in de README dat de add-on nooit iets verwijdert is hiermee vervallen.
- **Kookmomenten en restjes.** In plaats van "kookavonden" kies je kookmomenten, van 1 tot het aantal eetdagen. Zijn er meer eetdagen dan kookmomenten, dan worden de extra eetdagen restjesavonden ("<Dag> · restjes van <gerecht>"). Let op, dat is een ander gedrag dan voorheen: met 7 eetdagen en 5 kookmomenten zijn er nu 2 restjesavonden. De kookdag kookt voor zijn eigen avond plus de restjesavond (2 + 2 = 4 personen), nooit met vis, en er wordt niets extra voor gekocht. Een gerecht met restjes kan op tot 16 personen.
- **Menu vóór de nieuwe folder.** Nu maken maakt altijd een menu, ook als de folder van de bezorgweek er nog niet is (met de huidige folder en een waarschuwing). Komt de nieuwe folder later, dan krijg je één melding "De nieuwe folder is er" en toont de app bovenaan "Nieuwe folder is er" met de knop Menu bijwerken. Die maakt een nieuw menu op de nieuwe folder met dezelfde eetdagen, personen, restjes en weggehaalde dagen. De automatische ronde past een bestaand menu nooit zelf aan.
- **Tabblad Aanbiedingen.** De hele bonusfolder van de menuweek per categorie, met een zoekveld. Een plusknop zet het aantal dat de actie vraagt (bijvoorbeeld 2 bij 1+1 gratis) in de bestelling of op de lijst. Het telt mee in de besparing, op een eigen regel "waarvan losse aanbiedingen", en in het totaal. Een losse aanbieding wordt nooit opgeschoond.
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
