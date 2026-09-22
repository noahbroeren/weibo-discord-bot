# 718 Made — Weibo naar Discord bot

Checkt periodiek de Weibo van 718 Made op nieuwe posts, laat Claude de tekst
vertalen naar vloeiend Engels en bepalen om welk product het gaat (op basis van
de tekst en de foto's), en post het geheel naar `#updates` in het vaste formaat:

```
**Progress Update | CANADA GOOSE JACKETS**

De vertaalde tekst van de Weibo-post.
```

Met daaronder alle foto's en (indien aanwezig) de video als bijlage.

## Hoe het werkt

1. GitHub Actions draait elke 10 minuten.
2. Het script haalt de laatste posts op van `m.weibo.cn` (uid `5780980302`).
3. `state.json` bepaalt wat al verwerkt is, zodat niets dubbel geplaatst wordt.
4. Per nieuwe post: tekst + foto's gaan in een API-call naar Claude, die
   terugkomt met de Engelse vertaling en een producttitel in caps.
5. Alles wordt als een bericht met bijlagen naar de Discord webhook gestuurd.
6. `state.json` en `products.json` worden teruggecommit naar de repo.

### Producttitels consistent houden

`products.json` bevat alle titels die eerder gebruikt zijn. Die lijst wordt
meegestuurd in elke API-call, met de instructie om een bestaande titel te
hergebruiken als het om hetzelfde product gaat. Zo wordt dezelfde jas niet de
ene keer "CANADA GOOSE JACKETS" en de andere keer "CANADA GOOSE DOWN JACKET".

Nieuwe producten worden automatisch aan de lijst toegevoegd. Wil je een titel
corrigeren, pas dan gewoon `products.json` in de repo aan — de bot gebruikt
vanaf dat moment jouw versie.

Kan Claude echt niet bepalen om welk product het gaat (bijvoorbeeld bij een
algemene aankondiging), dan wordt de titel `718 MADE`.

## Setup

### 1. Discord webhook

Ga in Discord naar het updates-kanaal -> tandwiel -> Integraties -> Webhooks
-> Nieuwe webhook. Kopieer de webhook-URL.

### 2. Anthropic API key

Maak een key aan op console.anthropic.com. Zonder key wordt de post nog steeds
geplaatst, maar dan onvertaald en zonder producttitel.

### 3. Repository secrets

Settings -> Secrets and variables -> Actions -> New repository secret

| Naam | Waarde |
|---|---|
| `DISCORD_WEBHOOK_URL` | de webhook-URL uit stap 1 |
| `ANTHROPIC_API_KEY` | je Anthropic API key uit stap 2 |

Het Weibo user-id staat in `.github/workflows/weibo-to-discord.yml`.

### 4. Eerste run

De eerste run post bewust niets. Hij legt alleen vast wat op dat moment de
nieuwste post is, zodat de bot niet in een klap je hele Weibo-historie in het
kanaal dumpt. Vanaf de tweede run worden alleen echt nieuwe posts geplaatst.

Wil je toch handmatig testen of het posten werkt, zet dan tijdelijk in
`state.json` het id van een iets oudere post - de bot pakt dan alles op wat
daarna komt.

## Kosten

Per post: een API-call met max 4 verkleinde foto's plus wat tekst. Dat is een
fractie van een cent. Bij een paar posts per dag praat je over centen per maand.

Wil je het goedkoper, zet dan `ANTHROPIC_MODEL` in de workflow op een kleiner
model. Wil je betere productherkenning, kies dan een groter model.

## Bekende beperkingen

- Ongedocumenteerde Weibo-API: `m.weibo.cn` heeft geen officiele publieke API.
  Structuurwijzigingen of blokkades van cloud-IP's kunnen de bot tijdelijk
  stilleggen. Check bij problemen eerst het Actions-log.
- Productherkenning is een inschatting. Bij duidelijke merken en logo's zit
  Claude er meestal goed op, maar het blijft een gok bij vage posts. Controleer
  de eerste dagen even mee en corrigeer `products.json` waar nodig.
- 10 minuten interval: praktische ondergrens voor gratis GitHub Actions.
- Reposts: bij een repost van iemand anders wordt alleen de eigen tekst van
  718 Made meegenomen, niet de originele post.
- Video's boven 50MB worden niet geupload; in plaats daarvan komt er een link
  naar de originele Weibo-post onder het bericht.
- Berichten boven 2000 tekens worden afgekapt (Discord-limiet).

## Als Weibo blokkeert

Foutmelding in het Actions-log zoals `Kon geen geldige JSON van Weibo krijgen`
of `JSONDecodeError`? Dan stuurde Weibo geen JSON maar een blokkade- of
loginpagina terug. Het script print nu wat er precies terugkwam, dus kijk eerst
in het log naar de regel die begint met "poging 1/3".

Meest voorkomende oorzaak: Weibo blokkeert het datacenter-IP van de GitHub
Actions runner. Oplossing: geef de bot een ingelogde sessie mee via een cookie.

1. Log op je eigen computer in op m.weibo.cn in de browser.
2. Open de developer tools (F12) -> tabblad Network.
3. Ververs de pagina, klik op het eerste verzoek naar m.weibo.cn.
4. Zoek bij Request Headers de regel `Cookie:` en kopieer de hele waarde
   erachter (dat is een lange tekst met SUB=..., SUBP=... erin).
5. Zet die tekst als repository secret met de naam `WEIBO_COOKIE`.

De workflow pikt die secret automatisch op. Let op: zo'n cookie verloopt na
verloop van tijd (meestal weken tot maanden), dus als de bot ooit opnieuw stopt
met werken is dit het eerste om te vernieuwen.

Werkt ook dat niet, dan blokkeert Weibo de runner-IP's hardnekkig. In dat geval
is de bot beter af op een eigen VPS of op een machine die altijd aanstaat, want
die heeft een gewoon consumenten-IP.
