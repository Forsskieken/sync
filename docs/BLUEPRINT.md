# Live subtitlesync — MKV-weg — blueprint

## Doel

Jellyfin pikt losse `.srt`-bestanden op een SMB-share niet op, ingebedde ondertitels wel. Deze uitbreiding geeft de synctool daarom twee nieuwe mogelijkheden. Ten eerste: ondertitels die al **in** een MKV zitten opsommen, uithalen, met de bestaande voorkant bijregelen en na het opslaan terugmuxen. Ten tweede: video's met een losse `.srt` ernaast omzetten naar één MKV met de ondertitel erin, zonder te hercoderen — per bestand tijdens het kijken, of desgewenst een hele map in één keer (zie *Hoe dit gebruikt wordt*).

De bestaande weg (losse `.srt` naast de video, `write_srt` met `.orig`-backup) blijft ongewijzigd bestaan en werken. De Jellyfin-kant blijft buiten beschouwing.

## Hoe dit gebruikt wordt

**Dit is een hulpmiddel bij het kijken, geen migratieproject.** Guy pakt de tool op het moment dat een ondertitel niet klopt bij wat er op dat moment op de tv staat: bijregelen, opslaan, verder kijken. Ligt de ondertitel los naast de film, dan bouwt hij hem in één moeite door in de MKV, zodat Jellyfin hem voortaan wél oppakt.

Dat bepaalt de verhouding tussen de twee delen. **De webpagina is het hoofdgebruik**; de bulkweg (`convert.py`, §6) is gereedschap dat er is als het ooit nodig is. De collectie telt 13.443 video's en 17.338 ondertitels (gemeten 2026-09-10), waarvan er 2.573 geen taal in de naam hebben — die komen in dit tempo over jaren één voor één langs, en dan is één tik op een taaltegel de juiste hoeveelheid moeite. Er is dus geen achterstand die weggewerkt moet worden, en waar dit document spreekt over "zodat de collectie één vorm krijgt" is dat een mogelijkheid, geen opdracht.

**De bulkweg mág wel**, en is er niet voor niets: hij is bruikbaar om een enkele serie in één keer om te zetten voordat je eraan begint, of om ooit alsnog de hele collectie te doen. Twee dingen om dan te wegen, die per bestand niet spelen maar over 13.443 bestanden wel:

- Elke omzetting herschrijft de **hele container**, niet alleen de ondertitel. Over de volle collectie is dat vele terabytes schrijfwerk, uren tot dagen.
- De opslag is ZFS, dus snapshots zouden elke herschreven film in zijn oude vorm vasthouden en het ruimtegebruik van de collectie kunnen verdubbelen. **Gemeten 2026-09-10: er zijn geen snapshots** op `QData/QSerie` of `QData/QFilm` (`zfs list -t snapshot -r` geeft "no datasets available"), en er is 5,38 TB vrij tegenover 10,8 TB serie en 3,55 TB film. Zonder snapshots geeft ZFS de oude versie na de rename meteen vrij, dus een omzetting kost netto alleen de ondertitel — enkele honderden kilobytes. De piek blijft één bestandsgrootte, wat de ruimtecontrole in §3 per bestand al afvangt. **Een bulkrun is dus ruimtetechnisch veilig**; komen er later wél snapshots op deze datasets, dan geldt de waarschuwing hierboven alsnog.

## Aannames

Ingevuld omdat het niet gevraagd was; corrigeer waar het misgaat.

1. **De collectie is grotendeels al MKV met een losse `.srt` ernaast.** AVI/MP4 zijn de minderheid. Daarom behandelt het ontwerp "srt in een bestaande MKV steken" als hetzelfde werk als "AVI naar MKV omzetten" — één handeling, niet twee. Als jouw collectie juist grotendeels AVI is verandert dat niets aan het ontwerp, alleen aan welke tak het vaakst loopt.
2. **Eén gebruiker tegelijk.** De tool heeft geen login en draait op de loopback; er is nooit meer dan één mens aan het werk. Het ontwerp staat daarom hoogstens één zware taak tegelijk toe.
3. **De share is gemount met vaste uid/gid** (`uid=1001,gid=1003` of de `family`-gid uit de README), zodat eigendom van een nieuw geschreven bestand door de mount bepaald wordt en niet door `chown`.
4. **mkvtoolnix v92 op de ontwikkelmachine, v82 op CT 101** (gemeten). Beide liggen ruim boven 68, dus alle nieuwe vlagnamen zijn er; de versiedetectie blijft staan omdat ontwikkel en productie niet gelijk lopen en de oudere namen ooit nodig kunnen zijn.
5. **Vrije ruimte op de share is doorgaans ruim.** Muxen vraagt tijdelijk de volledige bestandsgrootte extra; het ontwerp controleert dat vooraf in plaats van er op te vertrouwen.
6. **`.srt` is de enige tekstvorm die volledig ondersteund wordt.** ASS/SSA/WebVTT krijgen een expliciete, aangekondigde omzetting naar SubRip (zie Beslist in ronde 2, §1).
7. **De collectie is tweetalig.** Ondertitels zijn vaak Nederlands, maar even vaak Engels, en bij uitzondering iets anders. Er bestaat dus geen zinvolle vaste taalcode: de taal wordt **per bestand** bepaald (zie Opbouw §3b). Een verkeerd label is geen schoonheidsfoutje — Jellyfin kiest zijn spoor op de taalcode, dus een Engelse ondertitel met het label `nld` betekent dat de speler voortaan het verkeerde spoor voorzet.
8. **De bestandsnaam is de betrouwbaarste bron.** `film.nl.srt`, `film.eng.srt`, `film.Dutch.srt`: het achtervoegsel klopt in de praktijk vrijwel altijd, en waar het ontbreekt is er geen andere bron zonder de tekst zelf te gaan lezen. Dit ontwerp leest de tekst niet (zie Buiten scope).

## Keuzes

### Taal: Python, ongewijzigd

Bestaand project, bestaande taal. Alle nieuwe zware arbeid gebeurt bovendien in externe programma's (`mkvmerge`, `ffprobe`); Python is hier alleen lijm en HTTP-laag. Er komt **geen enkele nieuwe Python-afhankelijkheid** bij — `requirements.txt` blijft zoals hij is.

*Verworpen alternatief:* de omzetter als los Go-binary. Dat zou de tabel uit `docs/werkwijze.md` volgen voor "CLI-tool", maar het zou een tweede taal in één project brengen om vervolgens dezelfde `subprocess`-aanroepen te doen én `parse_srt`, `safe_path` en de encoding-sniffer te moeten dupliceren. Duplicatie van juist die code is de gevaarlijkste soort duplicatie hier: als de twee implementaties uit elkaar lopen, verschilt het gedrag van de webweg en de bulkweg op een bestand dat onherroepelijk overschreven wordt.

### Werkverdeling mkvtoolnix / ffmpeg

Dit is de kern van de motivatie die gevraagd werd.

| Handeling | Programma | Waarom |
|---|---|---|
| Sporen opsommen | `mkvmerge -J` | Geeft JSON met **mkvmerge-track-id's**, codec-id, taal, naam en de default/forced-vlaggen. Dat zijn precies de id's die we straks weer aan mkvmerge teruggeven. |
| Tekstspoor uithalen | `mkvextract tracks <id>:<uit>` | Zelfde id-nummering als hierboven. Haalt een SubRip-spoor er byte-voor-byte uit, zonder te herinterpreteren. |
| Terugmuxen / omzetten | `mkvmerge` | De referentiemuxer voor Matroska: behoudt hoofdstukken, bijlagen (covers, fonts), tags, track-UID's, vlaggen en vertragingen. Geeft machineleesbare voortgang met `--gui-mode`. |
| Duur meten | `ffprobe` | Zit er al in (`probe()`, `video_info()`), werkt op containerniveau zonder spoornummers, dus geen kans op verwarring met de mkvmerge-nummering. |
| ASS/SSA → SRT omzetten | `ffmpeg` | Bestand naar bestand, geen spoornummers in het spel. |
| Audio uitpakken (bestaand) | `ffmpeg` | Blijft zoals het is. |

**Waarom niet alles met ffmpeg.** ffmpeg kán dit: `ffmpeg -i in.mkv -i new.srt -map 0 -map -0:s:1 -map 1 -c copy out.mkv` remuxt ook zonder te hercoderen. Twee redenen om het hier niet te doen. (a) *Trouw aan het origineel*: ffmpeg is als Matroska-muxer minder nauwkeurig, en het origineel wordt hier **definitief vervangen** — bij een omkeerbare bewerking zou dat gezeur zijn, hier niet. Gemeten op v92 door dezelfde MKV met beide te remuxen (`ffmpeg -map 0 -c copy`):

| Eigenschap | mkvmerge | ffmpeg |
|---|---|---|
| `language_ietf`, alle sporen | `und` / `nl` | **weg** |
| `default_duration`, audio | 23219954 | **weg** |
| `date_local` / `date_utc` | aanwezig | **weg** |
| Bijlagen en hoofdstukken | behouden | behouden |

Bijlagen en hoofdstukken overleven dus wél — dat stond hier eerst als argument en dat klopte niet. Het werkelijke verlies weegt zwaarder: **`language_ietf` verdwijnt volledig**, en dat is juist het veld waar `identify()` primair naar kijkt (zie §3b). Een collectie die één keer door ffmpeg geremuxt is, valt daarna voor de taalbepaling terug op het oude `language`-veld met zijn B-codes. (b) *Eén nummerstelsel*: door identificeren, uithalen én muxen allemaal via mkvtoolnix te doen, bestaat er maar één soort spoornummer in de hele codebase. Het mengen van ffmpeg-stream-indexen en mkvmerge-track-id's is precies de fout die het verkeerde spoor sloopt, en dat merk je pas als het origineel al weg is.

**Waarom niet alles met mkvtoolnix.** mkvmerge kan geen duur van een willekeurige container betrouwbaar rapporteren en kan geen ASS naar SRT omzetten. Bovendien werkt de bestaande audiofunctie al met ffmpeg; die blijft.

**Waarom niet `mkvmerge --sync`.** mkvmerge kan een spoor globaal verschuiven (`--sync 3:-1200`) en zelfs lineair schalen, dus voor een simpele verschuiving of een framerate-omzetting zou dat volstaan zónder de srt aan te raken. Toch niet gekozen: de voorkant kan óók "vanaf deze cue en alles erna" verschuiven, en dat is geen globale vertraging. Twee schrijfwegen naast elkaar (soms `--sync`, soms een vervangend spoor) geeft twee keer zoveel gedrag om te controleren, terwijl `--sync` niets bespaart — mkvmerge herschrijft het hele bestand hoe dan ook. Eén weg dus: altijd een vervangend spoor.

### Beeldondertitels

PGS en VobSub (`S_HDMV/PGS`, `S_VOBSUB`) worden opgesomd, maar niet aangeboden om te bewerken: de tool meldt "beeldspoor, geen tekst" en laat het spoor met rust. Bij een terugmux blijven ze gewoon meekopiëren. Geen OCR.

### Eén webpagina of los gereedschap?

**Beide, met de logica één keer.** De argumentatie:

- De *ene* handeling die je op het bestand doet waar je nu naar kijkt hoort in de pagina. Je hebt de aflevering net bijgeregeld, je ziet hem staan, je tikt één keer. Een aparte terminal openen voor dat ene bestand is onzin.
- Een *collectie* omzetten hoort niet in een browsertab. Tweehonderd bestanden van 5 GB is uren werk; een tab die al die tijd open moet blijven en waarvan een verdwaalde tik of een slapende telefoon de voortgangsweergave breekt, is de verkeerde verpakking. Zo'n run wil je kunnen starten, kunnen loskoppelen, achteraf een verslag van kunnen lezen, en vooral: **eerst kunnen proefdraaien zonder iets te schrijven**. Dat laatste is in een CLI één vlag en in een webpagina een heel scherm.
- De risicovolle code is in beide gevallen dezelfde vijftig regels (bouw commando → draai met voortgang → controleer → zet atomair op zijn plaats). Die staat in `mkv.py` en wordt door beide bediend.

Dus: `mkv.py` (de logica), een knop in `sync.html` voor het geopende bestand, en `convert.py` voor de bulk — standaard een proefrun.

## Opbouw

Nieuwe en gewijzigde bestanden in `/home/guyf/projecten/thuis/sync/`:

```
app.py         bestaand — endpoints erbij, srt- en padcode eruit verhuisd
subs.py        NIEUW — tc_to_ms, ms_to_tc, read_text, parse_srt, render_srt, write_srt, find_srts
lang.py        NIEUW — taalcodes normaliseren, de taal uit een bestandsnaam halen
paths.py       NIEUW — allowed_roots(), resolve_in_roots()
mkv.py         NIEUW — identificeren, uithalen, muxen, controleren, vervangen, taken
convert.py     NIEUW — CLI voor de bulkomzetting, standaard proefrun
sync.html      bestaand — spoorkeuze, taalkeuze, MKV-opslaan, omzetknop, voortgang
README.md      bestaand — installatie, configuratie, API, veiligheid
docs/BLUEPRINT.md  dit document
```

### 1. `subs.py` — verhuizing, geen herschrijving

`tc_to_ms`, `ms_to_tc`, `read_text`, `parse_srt` en `write_srt` gaan **ongewijzigd** uit `app.py` naar `subs.py`; `app.py` doet `from subs import ...`. Reden: `mkv.py` en `convert.py` hebben ze nodig, en `mkv.py` mag `app.py` niet importeren (kringimport, en `mkv.py` mag geen FastAPI kennen).

Eén toevoeging: `render_srt(cues) -> str` bevat het blokopbouw-stuk dat nu in `write_srt` zit; `write_srt` roept het aan. `mkv.py` gebruikt `render_srt` rechtstreeks, want dat pad wil geen `.orig`-backup naast een cachebestand.

Eén verharding in `render_srt`, apart te beoordelen (stap 2b): een cue waarvan de tekst een **lege regel** bevat breekt bij het teruglezen in twee blokken uiteen, want blokken worden op een lege regel gescheiden. Dat is een bestaand, sluimerend probleem; het wordt nu erger omdat de srt de invoer van een mux wordt. Oplossing: lege regels binnen een cuetekst inklappen tot één regelovergang, en het aantal keer dat dat gebeurde loggen op WARN.

**`find_srts()` verhuist mee**, uit `app.py` naar `subs.py`, want `convert.py` heeft precies dezelfde lijst nodig en mag `app.py` niet importeren. Hij krijgt daarbij twee wijzigingen. Ten eerste geeft elk item er een taalveld bij (`language`, `lang_source`, `forced`, `hearing_impaired`, `tag`), berekend met `lang.detect_srt()` uit §3b. Ten tweede een verharding: de huidige test is `f.name.startswith(video.stem)`, en die matcht ook `Aflevering 10.nl.srt` naast `Aflevering 1.mkv`, of `film2.en.srt` naast `film.mkv`. Voortaan moet wat er ná de stam staat ofwel leeg zijn (`film.srt`) ofwel beginnen met een scheidingsteken (`.`, `-`, `_` of een spatie). Dat is dezelfde grens die de taalontleding nodig heeft, dus het is één regel die twee problemen oplost. Een bestand dat door de nieuwe test valt wordt op DEBUG gelogd met de reden, want anders is "mijn srt is verdwenen uit de lijst" niet te verklaren.

### 2. `paths.py` — de padcontrole deelbaar maken

```
allowed_roots() -> list[Path]        leest ALLOWED_ROOTS uit de omgeving
resolve_in_roots(p, roots) -> Path | None
```

`resolve_in_roots` is exact de body van het huidige `safe_path`, maar geeft `None` in plaats van een `HTTPException`. In `app.py` blijft `safe_path` bestaan als tweeregelige omhulling die bij `None` `HTTPException(403, ...)` opgooit — dezelfde melding als nu, zodat de voorkant niets merkt. `convert.py` gebruikt `resolve_in_roots` rechtstreeks.

### 3. `mkv.py` — het hart

Kent geen FastAPI. Gooit `MkvError(msg)`, die `app.py` vertaalt naar een HTTP-fout.

**Identificeren.** `identify(path) -> dict` draait `mkvmerge -J <pad>` en levert per spoor: `id` (mkvmerge-id), `type`, `codec`, `codec_id`, `language`, `name`, `default`, `forced`, plus twee eigen velden:

- `editable: true` voor `S_TEXT/UTF8` (SubRip).
- `editable: "convert"` voor `S_TEXT/ASS`, `S_TEXT/SSA`, `S_TEXT/WEBVTT` — bewerkbaar, maar alleen door het spoor bij het opslaan door een SubRip-spoor te vervangen; opmaak en plaatsing gaan verloren. Vereist een aparte bevestiging.
- `editable: false` met `reason: "beeldspoor (PGS), geen tekst"` voor `S_HDMV/PGS` en `S_VOBSUB`.

Werkt ook op AVI/MP4 (mkvmerge leest die), zodat de omzetter dezelfde functie gebruikt.

**Uithalen.** `extract(path, track_id, dest) -> Path`, via `mkvextract tracks <id>:<dest>`. Bij ASS/SSA/WebVTT volgt een tweede stap `ffmpeg -i <dest.ass> <dest.srt>`. Uitkomst is altijd een `.srt` in `CACHE_DIR/embedded/`. De naam is `<sha1(pad:mtime_ns)>-t<id>.srt` — dezelfde sleutelvorm als `cache_key()` al gebruikt, dus een tweede keer openen van hetzelfde bestand is gratis.

Let op: uithalen leest praktisch het hele bestand. Op een grote MKV over CIFS is dat tientallen seconden. Het is dus **ook** een taak met voortgang, geen synchrone aanroep.

**Alle aanroepen met een vaste locale.** Elke aanroep van `mkvmerge`, `mkvextract` en `ffprobe` krijgt een expliciete `env` mee met `LC_ALL=C.UTF-8`. Dat is geen netheid maar noodzaak: op de ontwikkelmachine staat `LANG=C`, en mkvtoolnix v92 breekt daarop af met `terminate called after throwing an instance of 'std::runtime_error' — locale::facet::_S_create_c_locale name not valid` en afsluitcode 134. In een systemd-unit is de omgeving nog kaler dan in een inlogshell, dus dit gaat daar zeker spelen. Het levert bovendien voorspelbaar Engelstalige uitvoer op, wat de voorwaarde is om `#GUI#`- en `Progress:`-regels betrouwbaar te kunnen lezen.

**Muxen.** Eén bouwer voor alle drie de gevallen:

```
build_mux(source, drop_track_ids, add_srt, out, label) -> list[str]

label = {
  "language": "nld",          # altijd gezet, minstens "und"
  "track_name": "Nederlands", # None = geen --track-name
  "default": True,
  "forced": False,
  "hearing_impaired": False,
}
```

`label` komt uit §3b en is de enige plek waar taal en vlaggen vandaan komen; `build_mux` beslist niets zelf.

Concreet, spoor 3 van een MKV vervangen:

```
mkvmerge --gui-mode -o /mnt/Serie/X/.aflevering.mkv.syncpart \
         --subtitle-tracks !3 /mnt/Serie/X/aflevering.mkv \
         --language 0:nld --track-name 0:Nederlands --default-track-flag 0:1 \
         /var/cache/subtitle-sync/embedded/<jobid>.srt
```

Een losse srt in een bestaande MKV steken is hetzelfde zonder `--subtitle-tracks !3`. Een AVI omzetten is hetzelfde met `film.avi` als eerste invoer. De volgorde is dwingend: opties die op een invoerbestand slaan staan **vóór** dat bestand.

Versieafhankelijkheden, af te vangen door bij het opstarten één keer `mkvmerge --version` te lezen, het hoofdversienummer te bewaren en op INFO te loggen:

- `--default-track-flag 0:1` bestaat vanaf mkvtoolnix 68; daarvoor heet het `--default-track 0:1`.
- `--forced-display-flag 0:1` idem vanaf 68; daarvoor `--forced-track 0:1`.
- `--hearing-impaired-flag 0:1` bestaat pas vanaf 68 en heeft géén voorganger. Onder 68 wordt de vlag weggelaten en blijft alleen het achtervoegsel in de spoornaam over (`Nederlands (SDH)`), met één WARN-regel.
- Onder versie 50 wordt de tool geweigerd met een duidelijke melding in plaats van dat er onvoorspelbaar gedrag ontstaat.

De srt die de mux in gaat wordt **altijd door onszelf geschreven** met `render_srt`, ook bij de omzetter waar er al een `.srt` op schijf ligt. Dat lost de tekensetvraag in één keer op: `read_text()` snuffelt de codering (utf-8-sig, utf-8, cp1252, latin-1), `render_srt` schrijft UTF-8. Zo hoeft `--sub-charset` nooit gebruikt te worden en verdwijnt en passant de mojibake uit oude cp1252-bestanden. Wel loggen: aantal blokken in het bronbestand tegenover aantal geparste cues, want `parse_srt` gooit stille blokken weg.

**De vervangingsvolgorde.** Dit is het stuk dat je twee keer moet lezen voor je het bouwt.

1. `st = src.stat()`; onthoud grootte, mode en gid.
2. Duur van het origineel meten met `ffprobe` (`format=duration`).
3. Vrije ruimte controleren: `shutil.disk_usage(src.parent).free` moet minstens `st.st_size + 64 MiB` zijn (bij een omzetting ook nog de srt erbij). Zo niet: meteen weigeren met een melding die het tekort noemt. Dit is de enige manier om "schijf vol halverwege" grotendeels vóór te zijn in plaats van erna.
4. Verweesde brokken opruimen: alles in `src.parent` dat matcht op `.*.syncpart` **of `.*.syncold`** en ouder is dan 6 uur wordt verwijderd, met een WARN-regel per stuk. Alleen in díe map, dus geen dure wandeling over de hele share. Het `.syncold`-patroon hoort erbij omdat de terugvalweg in stap 8 zo'n bestand kan achterlaten wanneer de opruiming daarna faalt; zonder dit blijft dat voor altijd liggen met alleen een losse WARN als spoor.
5. Muxen naar `src.parent / f".{src.name}.syncpart"`. **Dezelfde map**, niet alleen hetzelfde filesystem — dat is de enige garantie dat de share en dus de rename dezelfde is. De naam begint met een punt (verborgen; `browse()` slaat punt-bestanden al over) en eindigt níet op `.mkv`, zodat Jellyfin het brok niet als nieuw item oppikt.
6. Controleren, in deze volgorde, en bij elke misser stoppen:
   a. Afsluitcode van mkvmerge is 0 of 1 (1 = waarschuwingen; die worden op WARN gelogd). 2 is een fout.
   b. `mkvmerge -J` op het brok: even veel videosporen, even veel audiosporen, en het aantal ondertitelsporen klopt met origineel − weggelaten + toegevoegd.
   c. Duur binnen 1,0 seconde van het origineel (`ffprobe`). Dit vangt een afgekapt bestand.
   d. Het nieuwe ondertitelspoor er weer uithalen naar `CACHE_DIR` en met `parse_srt` lezen: even veel cues als we erin stopten, geen enkele starttijd meer dan 2 ms afwijkend, **en de tekst van elke cue gelijk aan wat erin ging** (een hash over alle cueteksten volstaat). Die laatste eis is er in ronde 3 bij gekomen: zonder tekstvergelijking glipt een round-trip die de tekst corrumpeert maar aantal en tijden intact laat, ongemerkt door — precies het stille verlies waar deze controle voor bestaat. Vergelijk op de tekst zoals `render_srt` hem geschreven heeft, niet op de ruwe invoer, anders faalt de controle op het inklappen van lege regels dat we zelf doen.
   e. Bestandsgrootte: alleen een WARN-regel als het brok minder dan 70 % van het origineel is; geen weigering, want een AVI→MKV mag krimpen.
7. Rechten gelijkzetten: `os.chmod(tmp, stat.S_IMODE(st.st_mode))`, en `os.chown(tmp, -1, st.st_gid)` in een `try` — mislukt dat op de CIFS-mount, dan is dat normaal (de mount bepaalt het eigendom) en volstaat een DEBUG-regel.
8. `os.replace(tmp, doel)`. Eén systeemaanroep, atomair op een lokaal filesystem. Op CIFS mag dit mislukken met `EBUSY`/`EACCES` als de server het doelbestand nog open heeft — meestal omdat de speler het aan het streamen is. Terugvalweg: origineel hernoemen naar `.<naam>.syncold`, brok op zijn plaats zetten, oud bestand verwijderen. Mislukt dát ook, dan wordt het brok verwijderd, het origineel teruggezet en de taak faalt met "vervangen mislukte, mogelijk staat de speler nog op dit bestand — stop de weergave en probeer opnieuw". Het origineel blijft in élk faalgeval staan.
9. Bij een omzetting waar de doelnaam anders is (`film.avi` → `film.mkv`): eerst controleren dat `film.mkv` nog niet bestaat (zo ja: weigeren, want dat is een andere film of een halve vorige run). Na een geslaagde `os.replace` het bronbestand verwijderen. Mislukt die verwijdering, dan staan er twee bestanden — dat is een ERROR-regel met beide paden erin en de mededeling dat Jellyfin nu dubbel kan tonen, niet een stille toestand.
10. `finally`: het brok verwijderen als het er nog ligt, het cache-srt'je laten staan (goedkoop, wordt 's nachts opgeruimd).

**Taken.** Eén takenmodel voor uithalen én muxen:

```
Job = {id, kind, path, state, percent, phase, error, started, finished, log_tail}
kind  : "extract" | "replace-sub" | "ingest"
state : queued | running | verifying | done | error
```

`id` is een `uuid4().hex` — een mux is niet cachebaar zoals de audio, dus geen sleutel op inhoud. **Hoogstens één taak tegelijk, van welke soort ook**: een tweede aanvraag krijgt HTTP 409 met de naam van het bestand dat bezig is. Reden: twee volledige herschrijvingen tegelijk over dezelfde CIFS-share maken elkaar traag en maken de kans op een half-mislukte toestand groter, zonder dat er iets te winnen valt. Afgeronde taken blijven in een `deque(maxlen=20)` staan zodat de pagina de uitkomst nog kan tonen.

Voortgang: `mkvmerge --gui-mode` schrijft regels `#GUI#progress 42%`, en ook `#GUI#warning ...` en `#GUI#error ...` — die laatste twee gaan rechtstreeks in `log_tail` en in het journaal. `mkvextract` kent **ook** `--gui-mode` en schrijft dezelfde `#GUI#progress N%`-regels — gemeten op v92 door de optie echt uit te voeren. De optie staat bij geen van beide programma's in `--help`, dus `grep` op de helptekst bewijst niets; alleen uitvoeren telt. Er is dus één voortgangsweg voor uithalen én muxen, geen terugval nodig. (Zonder `--gui-mode` schrijft mkvmerge `Progress: N%` met carriage returns; dat blijft de noodweg als een oudere versie de optie niet kent.) Komt er geen voortgang, dan blijft de balk onbepaald in plaats van dat de taak faalt.

**Vastloper-bewaking.** Beweegt het percentage 15 minuten niet, dan wordt het kindproces gestopt en faalt de taak met "geen voortgang meer sinds ...". Eerlijk voorbehoud: bij een echt bevroren CIFS-mount hangt het proces in ononderbreekbare toestand en helpt geen enkel signaal. Dan blijft de taak op zijn percentage staan; dat stilstaande percentage plus de WARN-regels in het journaal zijn dan het signaal, en de mount zelf moet aangepakt worden.

**Idempotentie.** Een spoor vervangen met dezelfde cues levert twee keer hetzelfde bestand op — vanzelf ongevaarlijk. Een losse srt inbouwen is dat níet: twee keer draaien geeft twee ondertitelsporen. Daarom controleert `ingest` vooraf of het doel al een **tekstondertitelspoor met dezelfde sleutel** heeft. Die sleutel is `(genormaliseerde taalcode, forced-vlag)`:

- *Genormaliseerd* betekent door `lang.normalize()` heen. Een bestaand spoor met `dut` en een nieuwe srt met `nld` zijn dezelfde taal; zonder normalisatie zou elke tweede run een dubbel Nederlands spoor toevoegen. Dit is de belangrijkste reden dat de nld/dut-vraag geen kwestie van smaak is.
- De *forced-vlag* hoort erbij omdat `film.nl.srt` en `film.nl.forced.srt` twee verschillende dingen zijn die naast elkaar horen te bestaan. Zonder dat deel van de sleutel zou de tweede altijd overgeslagen worden.
- Een bestaand spoor met `und` telt nooit als gelijk aan een bekende taal, en omgekeerd. `und` is "we weten het niet", geen bewijs dat het juiste spoor er al staat.

Beeldsporen (PGS, VobSub) tellen niet mee in deze controle: die vervangen een tekstspoor niet. `--force` in de CLI zet de hele controle uit.

### 3b. `lang.py` — waar de taalcode vandaan komt

Eén vaste `DEFAULT_SUB_LANG` deugt niet: de collectie is deels Nederlands, deels Engels. Een vaste code betekent dat elke Engelse ondertitel `nld` als label krijgt, en Jellyfin kiest zijn spoor op dat label — je zou dus stelselmatig het verkeerde spoor voorgeschoteld krijgen, met een bestand dat inmiddels onherroepelijk herschreven is. De taal wordt daarom per bestand bepaald, en waar dat niet lukt wordt er niet geraden.

`lang.py` is een klein, zuiver module zonder bestandstoegang, zonder netwerk en zonder afhankelijkheden. Vier functies plus een tabel:

```
normalize(text) -> str | None     "nl" / "dut" / "NLD" / "Dutch" / "nederlands"  ->  "nld"
display(code)   -> str            "nld" -> "Nederlands", "und" -> "Onbekend"
parse_tag(tag)  -> dict           ".nl.forced" -> {language, forced, hearing_impaired, ...}
detect_srt(video_stem, srt_name) -> dict     het volledige oordeel over één bestand
selftest()      -> int            draait de tabel uit "Testen" hieronder
```

#### De drie gevallen

| Geval | Waar de taal vandaan komt | Als dat niets oplevert |
|---|---|---|
| **Bestaand spoor vervangen** (`/api/mkv/save`) | Taal, spoornaam en de default-, forced- en hearing-impaired-vlaggen van het **oude spoor**, uit `identify()`. Er wordt niets afgeleid en niets geraden: wat erin zat gaat er weer in. | Staat er `und` in het oude spoor, dan blijft dat `und`. Wel toont de pagina die taal als tikbaar, zodat je hem in dezelfde beurt kunt rechtzetten — het bestand wordt toch al herschreven, dus dat is gratis. |
| **Losse srt in een bestaande MKV** (`ingest`) | Het achtervoegsel van de srt-bestandsnaam, zie hieronder. | Web: vragen. Bulk: overslaan, tenzij `--lang` meegegeven is. |
| **AVI/MP4 omzetten** | Idem — precies dezelfde functie, dezelfde regels. | Idem. |

`identify()` leest de taal van een bestaand spoor uit `properties.language_ietf` wanneer die er is (mkvtoolnix vult dat sinds ±v45 in) en anders uit `properties.language`, en haalt beide door `normalize()`. Zo is een `dut`-spoor in een oud bestand en een `nld`-spoor in een nieuw bestand hetzelfde ding in de hele codebase.

#### De bestandsnaam ontleden

`find_srts()` selecteert op `f.name.startswith(video.stem)`. Wat er tussen de stam en `.srt` staat is dus precies het achtervoegsel dat we moeten lezen — het "label". Voor `film.mkv` en `film.nl.forced.srt` is dat `.nl.forced`.

De ontleding in vijf stappen:

1. **Grens controleren.** Het label is leeg (`film.srt`) of begint met `.`, `-`, `_` of een spatie. Begint het met iets anders, dan is het geen bijhorend bestand maar een ander bestand dat toevallig zo heet (`film2.en.srt` naast `film.mkv`); `find_srts()` laat het vallen (zie §1).
2. **In stukken hakken** op `.`, `-`, `_` en spaties, en alles in kleine letters zetten. `.nl.forced` wordt `["nl", "forced"]`.
3. **Regio-aanduidingen strippen.** `pt-br` en `en_us` zijn na stap 2 al `pt` + `br` en `en` + `us`; `br` en `us` vinden we in stap 4 niet terug als taal en verdwijnen als onbekend woord. Dat is precies goed: `pt-BR` wordt `por`, want de gewesttaal past niet in de ISO 639-2-code die Matroska in het oude taalveld verwacht.
4. **Elk stuk indelen** in één van vier soorten:
   - **taal** — staat in de tabel hieronder;
   - **kenmerk** — `forced`, `sdh`, `hi`, `cc`, `default`, `full`, `foreign`, `orig`, `original`;
   - **getal** — `1`, `2`, `01`: een teller, wordt genegeerd;
   - **onbekend woord** — al de rest (`subs`, `kopie`, `hearing`), wordt genegeerd zolang er ook een taal gevonden is.
5. **Oordelen.** Precies één taalcode gevonden → dat is de taal, `source: "name"`. Geen enkele → `source: "none"` als het label leeg was, `source: "unmatched"` als er wel iets stond maar niets herkenbaars. Twee of méér **verschillende** talen (`film.nl.en.srt`) → `source: "ambiguous"` en géén taal; dat is bewust conservatief, want zo'n naam betekent net zo goed "nl+en dubbel" als "de nl-versie van de en-release", en gokken kost hier een herschreven bestand.

Wat er van `.hi` gemaakt wordt is een **echte keuze en geen detail**: `hi` is zowel de ISO-code voor Hindi als de gangbare afkorting voor *hearing impaired*. In deze collectie wordt het als kenmerk gelezen, niet als taal, omdat een Hindi-ondertitel hier onwaarschijnlijker is dan een SDH-markering. Wie toch Hindi bedoelt schrijft `.hin`. Dit staat in de README onder *Embedded subtitles*, want het is precies zo'n regel die je een jaar later niet meer terugvindt.

De kenmerken zijn overigens geen ruis die je weggooit: `forced` en `sdh`/`hi`/`cc` gaan door naar het mux-commando als `--forced-display-flag` en `--hearing-impaired-flag` (§3, versieafhankelijkheden), en `forced` telt mee in de overslaan-sleutel (§3, Idempotentie).

#### De tabel

Vier ingangen per taal, allemaal naar dezelfde uitkomst:

| Vorm | Voorbeeld | Uitkomst |
|---|---|---|
| ISO 639-1, twee letters | `nl`, `en`, `fr`, `de` | `nld`, `eng`, `fra`, `deu` |
| ISO 639-2/T (terminologie) | `nld`, `eng`, `deu` | zichzelf |
| ISO 639-2/B (bibliografisch) | `dut`, `ger`, `fre`, `cze`, `gre`, `ice`, `chi`, `per`, `rum`, `slo`, `wel`, `alb`, `arm`, `baq`, `bur`, `geo`, `mac`, `may`, `tib` | de T-variant (`dut` → `nld`) |
| Naam, Engels of Nederlands | `dutch`, `nederlands`, `english`, `engels`, `french`, `frans`, `german`, `duits`, `spanish`, `spaans`, `flemish`, `vlaams` | `nld`, `nld`, `eng`, `eng`, … |

De tabel hoeft niet volledig te zijn. Neem de talen op die realistisch in de collectie voorkomen (nl, en, fr, de, es, it, da, sv, no, fi, pt, pl, tr, ru, ja, zh, ar) plus de B/T-paren hierboven, en laat de rest bewust op "onbekend woord" vallen. Een ontbrekende taal kost één regel in de tabel; een te gulle tabel kost een verkeerd label.

Uitkomst is **altijd** een ISO 639-2/T-code van drie letters, omdat het oude `Language`-element in Matroska niets anders aankan en elke mkvmerge-versie dat aanvaardt. `nld` dus, niet `dut` — maar het verschil is met de normalisatie onschadelijk gemaakt, en dát was het echte probleem in de vraag "nld of dut".

*Gemeten (v92):* `--language 0:nld` levert in het resultaat `language: "dut"` én `language_ietf: "nl"` op. mkvmerge vult het IETF-veld dus vanzelf — `--language-ietf` is niet nodig — maar schrijft in het oude veld de **B-code terug waar je een T-code instopte**. Daarmee is de normalisatie uit deze sectie geen randgeval maar het normale geval: elke MKV die deze tool zelf schrijft, leest hij daarna terug als `dut`. Zonder `normalize()` in de overslaan-controle zou de tweede run over dezelfde map er stelselmatig een dubbel Nederlands spoor bij muxen.

#### Wat er gebeurt als de taal niet vast te stellen is

Drie wegen liggen open. Ze zijn afgewogen op één vraag: welke fout is achteraf nog te herstellen?

- **`und` toekennen.** Aantrekkelijk omdat alles doorloopt, maar het is de enige weg die *stil* een fout vastlegt in een bestand dat daarna onherroepelijk vervangen is. En je komt er niet gemakkelijk van af: het spoor zit erin, de losse srt is er nog (die laten we staan), en een tweede run met een goede taalcode maakt er een dubbel spoor bij in plaats van het te herstellen. Afgewezen als automatisch gedrag; wel beschikbaar als bewuste keuze (een knop in de pagina, `--lang und` in de CLI).
- **De gebruiker laten kiezen.** In de webweg vanzelfsprekend: je staat er toch bij, het is één tik, en het gaat om het bestand dat je op dat moment voor je hebt.
- **Overslaan.** In de bulkweg het enige verantwoorde. Overslaan is niet hetzelfde als blokkeren: de run loopt gewoon door naar het volgende bestand, telt de overgeslagene, en noemt ze in de slotregel. Er verandert niets aan de schijf, dus je kunt na het lezen van `scan` de run herhalen met `--lang eng` voor de rest. Niets gaat verloren, alleen tijd.

Dus: **web vraagt, bulk slaat over, en `--lang <code>` is de bewuste ontsnapping.** `--lang eng` betekent "alles waarvan ik de taal niet kon vaststellen in deze run is Engels" — het overschrijft nooit een taal die wél gevonden is.

#### De voorkant

Kiezen moet met een tik kunnen: deze pagina wordt ook op een Nest Hub bediend, waar geen toetsenbord is. Dus geen invoerveld en geen keuzelijst met honderd talen.

- **Een rij van drie tegels**: `Nederlands`, `Engels`, `Andere…`. De eerste twee komen uit `SUB_LANG_BUTTONS`; `Andere…` klapt een tweede rij open met zes vaste tegels (Frans, Duits, Spaans, Italiaans, Portugees, `Onbekend (und)`). Twee tikken voor het zeldzame geval, één voor het gewone. Elke tegel toont de naam groot en de code klein eronder, in dezelfde stijl als de bestaande `.nudge`-knoppen met hun `<i>`.
- **De rij verschijnt alleen als er iets te kiezen valt.** Is de taal uit de naam gehaald, dan staat er één regel — `Taal: Nederlands · uit .nl` — en die regel is zelf het tikdoel dat de rij openklapt. Zo zie je altijd wat er gaat gebeuren zonder dat het scherm volloopt.
- **Geen voorgeselecteerde knop wanneer de taal onbekend is.** Dat is met opzet: met een voorselectie tik je "bevestigen, bevestigen, klaar" en heb je een Engelse ondertitel als Nederlands ingebouwd. De knop `Ondertitel inbouwen` blijft onklikbaar (`disabled`, zichtbaar grijs) met de tekst `kies eerst een taal` tot er getikt is.
- **Bij het vervangen van een bestaand spoor** staat er `Taal: Engels · van het bestaande spoor`, eveneens tikbaar. Wijzig je hem, dan verandert alleen `--language`; de default- en forced-vlaggen blijven die van het oude spoor.
- De taalkeuze staat in de bevestigingstekst van de tweede tik: `Bevestig · herschrijft 4,2 GB · spoor als Engels`. De taal hoort in dezelfde bevestiging als de omvang, niet ergens anders op het scherm.

#### De configuratie

`DEFAULT_SUB_LANG` **verdwijnt**, ook als terugval voor het naamloze geval. Juist daar is hij schadelijk: `film.srt` is precies het bestand waarvan we niets weten, en er dan stilzwijgend `nld` van maken is de fout die deze hele sectie wil vermijden. Er komt één variabele voor in de plaats:

| Variabele | Betekenis | Standaard |
|---|---|---|
| `SUB_LANG_BUTTONS` | JSON-lijst van taalcodes voor de eerste knoppenrij, in die volgorde | `["nld","eng"]` |

Dat is een voorkeur over *bediening*, niet over *labelen*: hij bepaalt welke twee tegels je meteen ziet en verder niets. Wordt hij leeg of ongeldig ingevuld, dan valt de code terug op de standaard met een WARN-regel; codes die `normalize()` niet kent worden overgeslagen met een WARN. De vorm sluit aan bij `PATH_MAP` en `ALLOWED_ROOTS`, die ook JSON in de omgeving zijn.

In de bulkweg is er geen omgevingsvariabele voor taal. Daar is `--lang` de enige weg, want een verkeerde waarde in een env-bestand die honderd bestanden ongemerkt verkeerd labelt is precies wat je niet wilt kunnen.

#### Waarom een eigen module, en wat er getest wordt

`lang.py` staat apart en niet in `subs.py`, om drie redenen. Het is de enige code hier die louter over *namen en metadata* gaat in plaats van over cues en tijdstempels. Alle vier de andere modules gebruiken hem (`subs.find_srts`, `mkv.ingest`, `app.py` voor de sporenlijst, `convert.py` voor `scan`), dus hij moet onderaan de importboom liggen — hij importeert zelf niets uit het project. En het is bij uitstek tabelcode die na verloop van tijd aangroeit; die groei wil je in één bestand hebben met de test ernaast.

Testen gebeurt met een `selftest()` in `lang.py` zelf, aan te roepen met `python3 lang.py --selftest`, dat de tabel hieronder afloopt en met afsluitcode 1 stopt bij een afwijking. Geen pytest: de belofte uit *Keuzes* is dat `requirements.txt` ongewijzigd blijft, en deze test heeft niets nodig.

| Bestandsnaam (video `film.mkv`) | Verwachte taal | Verwacht `source` | Bijzonder |
|---|---|---|---|
| `film.nl.srt` | `nld` | `name` | |
| `film.en.srt` | `eng` | `name` | |
| `film.dut.srt` | `nld` | `name` | B-code naar T-code |
| `film.nld.srt` | `nld` | `name` | |
| `film.eng.srt` | `eng` | `name` | |
| `film.Dutch.srt` | `nld` | `name` | hoofdletters, naamvorm |
| `film.srt` | — | `none` | het naamloze geval |
| `film.subs.srt` | — | `unmatched` | wel een label, niets herkenbaars |
| `film.nl.forced.srt` | `nld` | `name` | `forced: true` |
| `film.forced.nl.srt` | `nld` | `name` | volgorde maakt niet uit |
| `film.nl.sdh.srt` | `nld` | `name` | `hearing_impaired: true` |
| `film.hi.srt` | — | `unmatched` | `hi` is een kenmerk, geen Hindi |
| `film.hin.srt` | `hin` | `name` | zo bedoel je wél Hindi |
| `film.nl.en.srt` | — | `ambiguous` | twee talen |
| `film.nl.nld.srt` | `nld` | `name` | twee keer dezelfde taal is niet dubbelzinnig |
| `film.pt-BR.srt` | `por` | `name` | regio valt weg |
| `film.nl.2.srt` | `nld` | `name` | teller genegeerd |
| `film2.nl.srt` | (niet in de lijst) | — | grenscontrole uit §1 |
| `film-nl.srt` | `nld` | `name` | streepje als scheidingsteken |

Plus twee losse gelijkheidstests: `normalize("dut") == normalize("nld")` en `normalize("DUT") == normalize("Nederlands")`.

### 4. `app.py` — nieuwe endpoints

| Endpoint | Methode | Doet |
|---|---|---|
| `/api/mkv/tracks?path=` | GET | Sporenlijst met `editable` en `reason` per spoor |
| `/api/mkv/extract` | POST | `{path, track_id}` → start een taak, geeft `{job}` |
| `/api/mkv/job?id=` | GET | Toestand, percentage, fase, fout; bij een klare `extract` ook `cues` |
| `/api/mkv/save` | POST | `{path, track_id, cues, language?}` → start de mux. `language` ontbreekt = taal en vlaggen van het oude spoor behouden |
| `/api/mkv/ingest` | POST | `{video_path, srt_path, language?, drop_srt?}` → start de omzetting. `drop_srt` standaard `false` |

**Eén afgesproken foutvorm voor de taal.** Vraagt de voorkant een `ingest` aan zonder `language` terwijl de detectie niets opleverde, dan is het antwoord HTTP 422 met `{"detail": {"code": "language_unknown", "tag": ".subs", "reason": "unmatched"}}`. De voorkant herkent `code` en klapt de knoppenrij open in plaats van een rode melding te tonen; een mens leest `reason` en weet meteen of het om een leeg of om een onherkenbaar label ging. Een `language` die `lang.normalize()` niet kent geeft 400 — de tekst uit het verzoek wordt daarbij **niet** in de foutmelding herhaald, alleen de lengte, zodat een verzoek geen tekst in de pagina kan terugkaatsen.

Het uitgehaalde werkbestand ligt in `CACHE_DIR` en dus **buiten** `ALLOWED_ROOTS`. Het wordt daarom bewust niet via `/api/subtitle?path=` aangeboden — dat zou `safe_path` moeten verruimen, en die grendel blijft precies zoals hij is. In plaats daarvan geeft de klare extract-taak de cues rechtstreeks terug. De voorkant onthoudt dan `{origin:'embedded', mkv, track_id}` in plaats van een `subPath`, en stuurt bij het opslaan naar `/api/mkv/save`.

`/api/browse` en `/api/session` blijven ongewijzigd: er wordt géén `mkvmerge -J` gedraaid over een hele map, want dat is één procesopstart per bestand over de share. De sporenlijst wordt pas opgehaald wanneer één bestand geopend of één aflevering gestart wordt.

### 5. `sync.html` — voorkant

- **Eén keuzelijst.** De bestaande `#subSel` krijgt er de ingebedde sporen bij, met een voorvoegsel: `[3] Nederlands · SubRip (ingebed)` naast `aflevering.nl.srt`. De optiewaarde is `emb:3` voor ingebed en het pad voor los. `fillSubs()` wordt uitgebreid, `loadSub()` splitst op het voorvoegsel. Één bedienelement, dus ook op een tikscherm bruikbaar.
- **Sporen ophalen** gebeurt in `openEntry()` (lokale modus) en bij een nieuw `item_id` in `poll()` (tv-modus), altijd na `find_srts`, zodat een trage `-J` de rest niet ophoudt.
- **Uithalen toont voortgang** in dezelfde `.prog`-component die de audio al gebruikt, en `pollJob()` wordt veralgemeend naar `/api/mkv/job`.
- **Opslaan in twee tikken.** Zit de geladen ondertitel in een MKV, dan wordt de knop `Opslaan` → `In MKV opslaan`; de eerste tik verandert hem in `Bevestig · herschrijft 4,2 GB`, de tweede start. Na tien seconden zonder tweede tik valt hij terug. Geen `confirm()`-venster en geen invoerveld: op de Nest Hub is tikken het enige dat er is.
- **Drie tikken bij ASS/SSA.** Is het geladen spoor `S_TEXT/ASS` of `S_TEXT/SSA` (`editable: "convert"`), dan komt er vóór de gewone twee-tik-bevestiging een aparte: `Let op · opmaak en plaatsing gaan verloren` → `Ik weet het, ga door` → dan pas de normale bevestiging met de omvang. Bewust omslachtig: dit is de enige handeling in de tool die iets vernietigt wat niet in de cues zit en dus ook niet met een tweede sync terug te halen is.
- **Taalkeuze met tegels**, uitgewerkt in §3b onder *De voorkant*. Kort: één regel die de gevonden taal toont en zelf tikbaar is, een rij van drie tegels eronder wanneer er gekozen moet worden, `Andere…` voor een tweede rij, en geen voorselectie zolang de taal onbekend is.
- **Een voortgangsbalk boven de knoppenrij** (`#muxProg`, standaard verborgen) met percentage, fase en verstreken tijd, plus een geschatte resttijd uit `verstreken/percentage × (100−percentage)`. Werkt in beide modi, ook wanneer er geen bladerlijst is om iets onder te hangen.
- **Waarschuwing bij de spelende film.** Is het pad dat herschreven wordt gelijk aan `video_path` van de lopende sessie, dan staat er in de bevestigingstekst "de speler heeft dit bestand open — stop de weergave eerst". Het wordt niet geweigerd: de bestandsvervanging zelf is veilig, alleen de speler struikelt erover.
- **Omzetknop.** Is het geopende bestand géén MKV en is er een losse `.srt` bij, dan verschijnt in het lokale paneel `Naar MKV omzetten`, eveneens in twee tikken, met dezelfde voortgangsbalk. Ook aanwezig wanneer het al een MKV is met een losse srt ernaast; de knoptekst is dan `Ondertitel inbouwen`.
- **Hamburgermenu (optioneel, stap 12).** De pagina heeft er nog geen, terwijl dat wel de staande vorm is voor deze HTML-pagina's. Er hoort nu nog maar één instelling in: *losse srt na het inbouwen weggooien* (standaard nee). De taalcode hoort er uitdrukkelijk **niet** in — die is per bestand en staat bij de knop waar de handeling gebeurt, niet in een menu dat je één keer instelt en daarna vergeet. Aparte stap, apart te beoordelen.

### 6. `convert.py` — de bulkweg

```
convert.py scan  /mnt/Serie [/mnt/Film ...]
convert.py run   /mnt/Serie --apply [--drop-srt] [--lang eng]
                            [--limit N] [--min-free-gb 20] [--force]
```

- `scan` schrijft een tabel met zes kolommen: **pad**, **container**, **gevonden srt**, **taal**, **herkomst**, **actie**. De taalkolom toont de code en de herkomstkolom hoe we eraan komen (`uit .nl`, `uit .Dutch`, `geen label`, `label ".subs" niet herkend`, `twee talen in de naam`, `van het bestaande spoor`). Dit is de kolom die je vóór een `--apply` leest: klopt de detectie op een handvol regels, dan klopt ze op de rest, en zie je onzin staan dan pas je de naam aan of je gebruikt `--lang`.
- De actiekolom is één van: `omzetten (nld)`, `inbouwen (eng)`, `overslaan: taal onbekend`, `overslaan: heeft al nld`, `overslaan: heeft al nld+forced`, `overslaan: geen srt`, `weigeren: film.mkv bestaat al`. De actie noemt altijd de taal, want zonder taal zegt "inbouwen" niets over wat er in het bestand terechtkomt.
- `--lang <code>` geldt uitsluitend voor de bestanden waarvan de taal **niet** vastgesteld kon worden; een gevonden taal wordt er nooit door overschreven. `scan` toont het effect ervan wanneer de vlag meegegeven wordt, zodat `scan --lang eng` en `run --apply --lang eng` dezelfde lijst opleveren.
- Zonder `--lang` worden onbepaalde bestanden **overgeslagen, niet gelabeld en niet geblokkeerd**: de run loopt door en de slotregel noemt hun aantal. De redenering staat in §3b.
- `run` **zonder `--apply` is een proefrun** en raakt niets aan. Dat is de belangrijkste veiligheidseigenschap van dit onderdeel en de reden dat de bulkweg geen webpagina is.
- Elk bestand loopt door precies dezelfde `mkv.ingest()` als de webweg; het verschil is alleen de bediening en dat de CLI bestanden na elkaar afwerkt.
- Elk bestand is een op zichzelf staande transactie. Wordt de run met Ctrl-C of een herstart afgebroken, dan is het bestand dat bezig was onaangeroerd (het brok wordt in `finally` opgeruimd) en zijn de eerdere klaar. Opnieuw starten pikt vanzelf op waar het gebleven was, dankzij de overslaan-controle. Er is dus geen voortgangsbestand nodig — **de schijf is de toestand**.
- Stopt bij minder dan `--min-free-gb` vrije ruimte, met een duidelijke regel, in plaats van door te ploeteren tot de schijf vol is.
- Aan het eind: één samenvattingsregel `n omgezet, n ingebouwd, n overgeslagen (waarvan n taal onbekend), n mislukt, totaal x GB, y min` en afsluitcode 1 als er iets mislukte. "Taal onbekend" staat er apart in omdat dat het enige getal is waar je zelf iets aan kunt doen: het betekent "draai nog eens met `--lang`" en niet "er is iets stuk". Staat `HA_WEBHOOK_URL` in de omgeving, dan gaat diezelfde samenvatting als POST naar Home Assistant; staat hij er niet, dan gebeurt er niets en is dat geen fout.

## Werking bij fouten

**Logging.** `app.py` logt vandaag niets. Dat kan niet blijven zodra er bestanden onherroepelijk overschreven worden. Er komt een `logging.basicConfig(level=INFO, format="%(levelname)s %(name)s %(message)s")` naar stderr; journald vangt het op (`journalctl -u subtitle-sync -f`). Per taak in elk geval:

- INFO bij start: soort, pad, grootte in MB, spoor-id, duur van het origineel, vrije ruimte.
- INFO bij elke taalbepaling, één regel: `taal nld uit ".nl" (film.nl.srt)` of `taal eng opgelegd met --lang (film.srt, geen label)` of `taal onbepaald, overgeslagen (film.srt, geen label)`. Dit is de goedkoopste manier om achteraf te reconstrueren waarom een spoor het label heeft dat het heeft — het bestand zelf vertelt alleen de uitkomst, niet de reden.
- INFO per fase: `uithalen`, `muxen`, `controleren`, `vervangen`.
- INFO bij elke geslaagde controle met de gemeten waarden erbij (`duur 2718,4 s tegen 2718,5 s`, `312 cues, grootste afwijking 1 ms`), niet alleen "ok" — bij een latere twijfel wil je de getallen zien.
- INFO bij afloop: `vervangen: <pad>, 4210 MB → 4213 MB, 247 s`.
- WARN voor: mkvmerge-waarschuwingen, opgeruimde verweesde brokken, mislukte `chown`, ingeklapte lege regels, grootteafwijking, en het verschil tussen blokken in het bronbestand en geparste cues.
- ERROR bij falen, met: welke fase, welk pad, welke controle faalde met welke waarden, de laatste 300 tekens stderr van mkvmerge, en altijd de zin **"origineel ongewijzigd"** of, in het zeldzame geval van stap 9, precies welke twee bestanden er nu staan.

Geen wachtwoorden of tokens in de logs. Bestandspaden staan er wel in — dat is nodig om iets te kunnen naslaan, en de logs zijn lokaal.

**Herstarts.** De unit heeft al `Restart=on-failure` en `RestartSec=5`. Toevoegen: `StartLimitIntervalSec=300` en `StartLimitBurst=5`, zodat een stukke versie niet eindeloos herstart. Een herstart tijdens een mux betekent: kindproces sterft, brok blijft liggen, origineel is onaangeroerd. Het brok wordt opgeruimd zodra er in diezelfde map weer gewerkt wordt (stap 4 hierboven), en anders met de hand — de README noemt het patroon `.*.syncpart` zodat je het kunt terugvinden.

**Hervatbaarheid.** Een mux is **niet** hervatbaar en dat is een bewuste keuze: mkvmerge kan niet verder in het midden van een bestand, en een half brok is waardeloos. Wat wél gegarandeerd is, is dat de handeling een transactie is — of ze rondt af, of er verandert niets. Voor de bulk is de eenheid van hervatting één bestand, en de overslaan-controle maakt het opnieuw starten van een run gratis.

**Rapportage.** In de pagina: de balk en het toastbericht. In het journaal: de INFO-samenvatting per taak. Voor de bulk: de slotregel, de afsluitcode en optioneel de melding aan Home Assistant. Er is geen stille afloop — elke taak eindigt in `done` of `error`, en `error` betekent altijd een ERROR-regel plus een rood veld in de pagina.

**Wat wel en niet opnieuw proberen.** Niets wordt automatisch herhaald. Muxen is duur en de oorzaken van falen (schijf vol, ongeldige codec-combinatie, bestand in gebruik, bevroren mount) lossen zichzelf binnen enkele seconden niet op; automatisch herhalen zou de share alleen maar zwaarder belasten en het probleem verbergen. De gebruiker beslist. Enige uitzondering: de terugvalweg bij `os.replace` in stap 8, en dat is één poging, geen lus.

## Veiligheid

- **Geen nieuwe geheimen** voor deel 1 en 2. Komt de HA-melding er, dan is `HA_WEBHOOK_URL` het tweede geheim: alleen uit `subtitle-sync.env` (mode 600, buiten git), nooit in een logregel, en de URL wordt bij het loggen tot het schema en de hostnaam ingekort.
- **Alle paden door `resolve_in_roots`**: het MKV-pad, het srt-pad, én het afgeleide doelpad (`film.avi` → `film.mkv`) worden elk apart gecontroleerd, en bovendien wordt gecontroleerd dat de doelmap gelijk is aan de bronmap. Een symlink of `..` kan er dus niet uit lopen, ook niet via het afgeleide pad.
- **`track_id` is een `int` in het pydantic-model** en moet daarna nog voorkomen in de sporenlijst van dít bestand. Er gaat nooit door de gebruiker aangeleverde tekst in een mkvmerge-selector.
- **De taalcode is invoer van buiten en gaat door een witte lijst.** Ze komt uit een bestandsnaam op de share of uit een HTTP-verzoek, en belandt in een argument van `mkvmerge`. Alleen een code die `lang.normalize()` teruggeeft wordt doorgegeven; die uitkomst is per definitie een van de codes uit de eigen tabel, dus drie kleine letters. Aangeleverde tekst gaat nooit rechtstreeks een `--language`- of `--track-name`-argument in. De spoornaam wordt evenmin uit de bestandsnaam overgenomen maar met `lang.display()` uit dezelfde tabel gehaald — dat scheelt en passant dat een bestandsnaam met rare tekens in de metadata van je mediabestand terechtkomt.
- **Alleen bekende extensies**: invoer moet `.mkv`, `.mp4`, `.m4v`, `.avi`, `.ts` of `.webm` zijn — dezelfde lijst die `browse()` al hanteert.
- **Externe programma's met een argumentenlijst**, nooit een commandostring, nooit `shell=True`. De cuetekst en de spoornaam kunnen spaties, aanhalingstekens en accenten bevatten; in een lijst maakt dat niets uit. Alle paden zijn absoluut (`resolve()`), dus geen enkel argument kan per ongeluk als optie gelezen worden omdat het met een streepje begint.
- **Cuetekst wordt niet uitgevoerd, wel opgeschoond**: lege regels binnen een cue worden ingeklapt (anders splitst het blok bij het teruglezen) en de tekst gaat als UTF-8 naar een bestand dat wij zelf schrijven.
- **Niet als root**, ongewijzigd. De dienst draait als `jan:family`.
- **De unit hoeft niet aangepast te worden voor de rechten**: het brok staat in dezelfde map als het origineel en valt dus onder de bestaande `ReadWritePaths=`, die per README al gelijk moet zijn aan `ALLOWED_ROOTS`. `CACHE_DIR` valt onder `CacheDirectory=`. Dat is een prettige eigenschap om te bewaken: als een latere versie een brok elders wil neerzetten, breekt de sandbox dat zichtbaar af.
- **Aanvalsoppervlak**: vijf nieuwe endpoints zonder login op de loopback. Wie de poort bereikt kan nu niet alleen ondertitels overschrijven maar ook mediabestanden herschrijven en, via `ingest`, een bronbestand laten verwijderen. Dat verhoogt de inzet van de bestaande keuze om op `127.0.0.1` te blijven aanzienlijk. De README moet dat expliciet zeggen: de tweede `ExecStart`-regel (`0.0.0.0`) inschakelen betekent voortaan dat iedereen op het netwerk je collectie kan herschrijven, niet alleen je ondertitels.
- **Geen destructieve bulk zonder vlag**: `convert.py` schrijft pas iets bij `--apply`.

## Beslist in ronde 2

De vijf punten uit de eerste ronde zijn beantwoord. Ze staan hier als korte verantwoording; het gedrag zelf is in de Opbouw verwerkt.

1. **ASS/SSA-sporen.** Bewerkbaar na een **expliciete tweede bevestiging**; bij het opslaan wordt het spoor vervangen door SubRip en gaan opmaak en plaatsing verloren. De derde weg (tijdstempels in het ASS-bestand zelf herschrijven, al de rest onaangeroerd) blijft buiten scope tot blijkt dat het vaak voorkomt. Uitwerking: Opbouw §3 (`editable: "convert"`) en §5 (drie tikken).
2. **De losse `.srt` na een geslaagde inbouw** blijft staan. Hij is onschadelijk — Jellyfin negeert hem toch, dat was de aanleiding — en hij is de enige snelle weg terug als de mux achteraf tegenvalt. De vlag heet daarom `--drop-srt` en niet `--keep-srt`, en de webweg stuurt `drop_srt: false` tenzij het hamburgermenu anders zegt.
3. **Nieuw ondertitelspoor als standaardspoor.** Bij `ingest`: de default-vlag wél zetten wanneer er nog geen ander tekstspoor in het bestand zit, níet wanneer er al tekstsporen zijn — dan zou je een bestaande keuze overrulen. Bij het vervangen van een bestaand spoor: de vlag van het oude spoor overnemen. Eén toevoeging die uit §3b volgt: een spoor met de forced-markering krijgt **nooit** de default-vlag, ook niet als het het eerste tekstspoor is; een geforceerd spoor toont alleen de anderstalige stukken en is dus nooit bedoeld als het spoor dat standaard aanstaat.
4. **Bulkrun als timer:** nog niet. Eerst een paar keer met de hand draaien en de logs lezen. Zie Buiten scope.
5. **Taalcode.** Eén vaste code is verworpen — de collectie is deels Nederlands, deels Engels, en een vast label zet Jellyfin structureel op het verkeerde spoor. De taal wordt per bestand bepaald; de nld/dut-vraag is opgelost door te normaliseren in plaats van te kiezen. Volledig uitgewerkt in Opbouw §3b; `DEFAULT_SUB_LANG` verdwijnt en `SUB_LANG_BUTTONS` komt ervoor in de plaats.

## Nog open

1. ~~Wat zit er onder `/mnt/QData` op de PVE-host?~~ **Beantwoord: ZFS.** `findmnt --target /mnt/QData/QSerie` op de PVE-host geeft `zfs`, dataset `QData/QSerie`, `rw,noatime,xattr,posixacl`. Productie is CT 101 (`Docker`, 192.168.40.100), een *unprivileged* LXC op Proxmox waarin de dienst native met systemd draait — de installatieopzet van dit ontwerp en van de README klopt dus, geen container-image. De shares komen binnen als Proxmox-bind-mounts (`mp0: /mnt/QData/QSerie,mp=/mnt/Serie`). Gevolgen:
   - **`os.replace` is echt atomair.** Het is een gewone rename binnen één ZFS-dataset, geen netwerkbestandssysteem. De `EBUSY`-terugvalweg in stap 8 van de vervangingsvolgorde is daarmee overbodig; laat hem staan als goedkope verzekering, maar hij hoort niet de hoofdweg te zijn en de foutmelding mag niet meer over CIFS spreken.
   - **Het muxen gaat over lokale opslag, niet over het netwerk.** Waar dit document "herschrijft het hele bestand over een CIFS-share" zegt, blijft de I/O-kost gelden maar vervalt de netwerklatentie. Een mux is dus sneller dan hier begroot.
   - **ZFS is copy-on-write, dus de ruimtecontrole in stap 3 blijft nodig** — sterker nog, `shutil.disk_usage` rapporteert op ZFS de vrije ruimte van de pool, wat met een quota of reservering op de dataset kan afwijken van wat je werkelijk mag schrijven. Marge houden.
   - De idmap van CT 101 mapt `u 1001 1001 1` en `g 1003 1003 1`, dus uid 1001 en gid 1003 zijn identiek binnen en buiten de container en het eigendom klopt vanzelf.

2. **De mediashares staan niet op de ontwikkelmachine.** Op 192.168.40.250 zijn alleen `/mnt/Developing`, `/mnt/Politiek`, `/mnt/gesture` en `/mnt/homeassistant` gemount; `/mnt/Serie` en `/mnt/Film` niet. De stappen 0 tot en met 8 werken op zelfgemaakte testbestanden in `~/mkvtest` en hebben ze niet nodig, maar **stap 9 (`convert.py scan` over een echte map) en de meting in punt 3 kunnen daar niet uitgevoerd worden.** **Besloten: stap 9 wordt op CT 101 gedraaid**, waar de shares wel staan. De stappen 0 tot en met 8 blijven op de ontwikkelmachine.

3. ~~Hoeveel bestanden hebben een kale `film.srt` zonder taalaanduiding?~~ **Gemeten op 2026-09-10 over de echte collectie** (`convert.py scan /mnt/Serie /mnt/Film` op CT 101): 13.443 video's, 17.338 bijbehorende srt-bestanden. Daarvan 2.475 zonder enige taalaanduiding, 98 met een onherkend label en 0 met twee talen in de naam — samen **2.573 van 17.338, oftewel 15%**.

   Daarmee is de vraag beslist: de naamdetectie dekt 85% en de bulkweg is bruikbaar zoals ontworpen. Maar 15% is te veel om met de hand af te doen (2.573 bestanden à tien seconden is zeven uur), dus punt 4 hieronder komt daaruit voort.

4. ~~Taalherkenning op de inhoud~~ — **besloten: niet bouwen.** Guy geeft de taal zelf op met `--lang`, per map of per serie, voor de gevallen waarin hij weet wat het is. Dat is precies waar die vlag voor gemaakt is: hij geldt uitsluitend voor bestanden zonder taal uit de naam en raakt de overige 85% niet aan. Ondertitels hernoemen blijft eveneens buiten scope: `film.srt` blijft `film.srt`, de taal wordt alleen gebruikt bij het inbouwen.

   Daarmee zijn alle ontwerpvragen beantwoord en staat er niets meer open.

## Stappen voor de implementer

Bouw en controleer in deze volgorde. Ontwikkelen gebeurt op **CT 300** (`ssh ct300`).

**0. Testmateriaal maken, vóór alles.** Raak de echte collectie niet aan tot stap 9.

```bash
mkdir -p ~/mkvtest && cd ~/mkvtest
ffmpeg -f lavfi -i testsrc=size=320x240:rate=25 -f lavfi -i sine=frequency=440 \
       -t 30 -c:v libx264 -c:a aac -shortest test.mp4
printf '1\n00:00:01,000 --> 00:00:03,000\nEerste regel\n\n2\n00:00:05,000 --> 00:00:07,000\nTweede regel\n' > test.nl.srt
mkvmerge -o test.mkv test.mp4 --language 0:nld test.nl.srt
cp test.mp4 test.avi 2>/dev/null; ffmpeg -i test.mp4 -c:v mpeg4 -c:a mp3 test.avi
```

Draai de dienst tijdens het ontwikkelen met de hand (`ALLOWED_ROOTS='["/home/guyf/mkvtest"]' .venv/bin/uvicorn app:app --port 8099`), niet via de unit — `ProtectHome=yes` sluit je thuismap anders af. *Controle:* `mkvmerge -J test.mkv | head -40` toont drie sporen.

**1. `mkvtoolnix` installeren en de versie vaststellen.** Op CT 300, als root: `apt install -y mkvtoolnix` (het CLI-pakket; `mkvtoolnix-gui` is niet nodig). *Controle:* `mkvmerge --version` en `mkvextract --help | grep -c gui-mode` — noteer of `--gui-mode` bij `mkvextract` bestaat, want daar hangt de voortgangsafhandeling van stap 4 aan.

**2. `subs.py` en `paths.py` afsplitsen.** Functies verplaatsen, `app.py` laten importeren, verder niets veranderen. *Controle:* de dienst start, `/api/subtitle?path=...` geeft dezelfde cues als vóór de verhuizing, en `/api/subtitle/save` maakt nog steeds een `.orig`-backup. Vergelijk de uitvoer van beide endpoints vóór en na met `diff`.

**2b. `render_srt` met het inklappen van lege regels** (aparte commit, zodat je hem apart kunt terugdraaien). *Controle:* een cue met een lege regel erin, opgeslagen en teruggelezen, blijft één cue; de WARN-regel verschijnt in het journaal.

**2c. `lang.py` plus de aangepaste `find_srts()`.** Bouw de tabel, de vier functies en `selftest()`. Dit is de enige stap die volledig zonder mediabestanden te controleren is, dus doe hem vóór al het muxwerk. *Controle:* `python3 lang.py --selftest` loopt de tabel uit §3b af en geeft afsluitcode 0. Maak daarna in `~/mkvtest` de bestanden `test.srt`, `test.nl.srt`, `test.en.forced.srt`, `test.subs.srt` en `test2.nl.srt` aan (leeg mag) en controleer dat `/api/browse?path=/home/guyf/mkvtest` bij `test.mkv` vier ondertitels toont met de juiste taal en herkomst, en `test2.nl.srt` **niet** — die valt door de grenscontrole.

**3. `mkv.identify()`.** *Controle:* `/api/mkv/tracks?path=/home/guyf/mkvtest/test.mkv` toont het SubRip-spoor als `editable: true`. Maak ook een MKV met een PGS-spoor (of test tegen een echte Blu-ray-rip, alleen lezend) en controleer dat die `editable: false` krijgt met een leesbare reden.

**4. Taakmodel plus `mkv.extract()`.** Inclusief de ene-taak-tegelijk-grendel en `/api/mkv/job`. *Controle:* een extract op `test.mkv` levert de twee cues terug; een tweede aanvraag terwijl de eerste loopt geeft 409 met de bestandsnaam erin. Op een groot echt bestand (alleen lezend!) loopt het percentage op.

**5. `build_mux()` plus de controlestappen 6a–6e, nog zónder te vervangen** — schrijf naar een brok en laat het staan. **Maak eerst een nieuw testbestand met `mkvmerge`**, niet met ffmpeg, en zet er minstens één bijlage en hoofdstukken in: de hele motivatie voor mkvtoolnix (zie *Werkverdeling*, punt a) is dat ffmpeg juist die dingen niet trouw overneemt, dus een door ffmpeg gemaakt testbestand heeft niets te verliezen en bewijst hier niets. *Controle:* het brok speelt af in `mpv`, `mkvmerge -J` toont de gewijzigde tijden, en de vier controles loggen hun gemeten waarden. Forceer expres een misser: mux met een afgekapte srt en zie dat controle (d) aanslaat.

**6. De vervangingsvolgorde (stappen 1–10 uit de Opbouw).** *Controle:* op `test.mkv` een verschuiving van 2 s opslaan; het bestand is vervangen, `mkvextract` toont de nieuwe tijden, er staat geen `.syncpart` meer in de map, en het journaal toont de samenvattingsregel. Test daarna de faalweg: maak de map tijdelijk alleen-lezen (of vul een kleine loop-mount) en controleer dat het origineel ongemoeid blijft en de ERROR-regel "origineel ongewijzigd" bevat.

**7. `/api/mkv/save` erbij en de voorkant voor deel 1.** Keuzelijst met ingebedde sporen, twee-tik-bevestiging, voortgangsbalk. *Controle:* van de bank af, met de echte tv: een aflevering met een ingebed spoor bijregelen, opslaan, en na het opnieuw starten van de weergave staat de ondertitel goed. Let op de waarschuwing dat de speler het bestand open heeft.

**8. `mkv.ingest()` en `/api/mkv/ingest`**, inclusief de taalbepaling, de overslaan-controle op `(taal, forced)` en het weigeren wanneer `film.mkv` al naast `film.avi` staat. *Controle:* (a) `test.avi` + `test.nl.srt` wordt `test.mkv` met een spoor `nld`, naam `Nederlands`, default-vlag aan — en de losse srt staat er nog. (b) Twee keer draaien geeft de tweede keer "al gedaan" en geen tweede spoor. (c) Maak met de hand een MKV met een spoor dat `dut` als taal heeft en bied er `test.nl.srt` bij aan: die moet óók overgeslagen worden — dat is de normalisatietest, en als die faalt krijgt de hele collectie dubbele sporen. (d) `test.en.forced.srt` in datzelfde bestand wordt wél ingebouwd (andere sleutel), met de forced-vlag en zonder de default-vlag. (e) `test.srt` zonder label geeft HTTP 422 met `code: "language_unknown"`, en met `language: "eng"` erbij lukt het alsnog. Controleer telkens met `mkvmerge -J | grep -i language`.

**9. `convert.py`,** eerst alleen `scan`, dan `run` zónder `--apply`, en pas als de proefrun over een echte map klopt met wat je verwacht, `--apply` op één map met een handvol bestanden. *Controle:* de tabel van `scan` klopt met wat er in de map staat, en de taal- en herkomstkolom zijn regel voor regel na te lopen — dit is het moment om vast te stellen hoeveel bestanden `overslaan: taal onbekend` krijgen (zie *Nog open* §1). De proefrun schrijft aantoonbaar niets (`find /mnt/... -newer` na afloop is leeg). Draai daarna `scan --lang eng` en controleer dat alleen de onbepaalde regels van actie veranderen en geen enkele bestaande taal wijzigt. De eerste echte run levert bestanden op die Jellyfin met de **juiste** ondertiteltaal toont — controleer dat in Jellyfin zelf, niet alleen met `mkvmerge -J`, want daar zie je pas of het label ook doet wat het moest doen.

**10. Unit bijwerken**: `StartLimitIntervalSec=300` en `StartLimitBurst=5`. *Controle:* `systemd-analyze verify /etc/systemd/system/subtitle-sync.service` en een bewust stukke start herstart niet eindeloos.

**11. README bijwerken** (Engels, anoniem, hoogstens twee regels per item):
- Onder *What it does*: twee punten, één voor het bijregelen van ingebedde ondertitels, één voor het omzetten naar MKV.
- Onder *Install*: `mkvtoolnix` bij `apt install`, met de opmerking dat het GUI-pakket niet nodig is en dat mkvtoolnix ≥ 68 de nieuwere vlagbenaming heeft.
- Een nieuw kopje *Embedded subtitles*: hoe een spoor gekozen wordt, wat er met ASS en met PGS gebeurt, en in drie regels de veiligheidsvolgorde — brok in dezelfde map, controleren op sporen, duur en leesbaarheid, dan pas atomair vervangen, en bij twijfel blijft het origineel staan. Vermeld dat er **geen backup** achterblijft en dat de speler het bestand moet loslaten.
- Een nieuw kopje *Converting to MKV*: the web button for one file, `convert.py` for the rest, that a run without `--apply` is a dry run, and that the loose `.srt` is kept unless `--drop-srt` is given.
- Een nieuw kopje *Subtitle language*, hoogstens twee regels per punt: the language comes from the filename suffix (`film.nl.srt`, `film.eng.srt`, `film.Dutch.srt`); `dut` and `nld` are the same language to this tool; `.forced`, `.sdh`, `.hi` and `.cc` are markers, not languages, so `.hi` means hearing impaired and Hindi must be written `.hin`; a file whose language cannot be determined is asked about in the browser and skipped in bulk unless `--lang` is given. Vermeld ook of `mkvmerge` het IETF-taalveld vanzelf invult (uitkomst van de controle in stap 8).
- Bij *Configuration*: `SUB_LANG_BUTTONS` (JSON list, which language tiles the page offers — a display preference, it never labels anything by itself) en eventueel `HA_WEBHOOK_URL`. `DEFAULT_SUB_LANG` bestaat niet: er is geen vaste taal.
- Bij *Security*: één zin die zegt dat het openzetten naar het netwerk nu ook betekent dat mediabestanden herschreven en verwijderd kunnen worden.
- Bij *The API*: de vijf nieuwe rijen.
- Ergens kort: brokken heten `.<naam>.syncpart` en mogen na een crash met de hand weg.

**12. Optioneel: hamburgermenu** in `sync.html` met één instelling: *losse srt weggooien na het inbouwen* (standaard uit). De taal hoort er niet in — die staat per bestand bij de knop, zie §5. *Controle:* op de Nest Hub met één tik te openen en de keuze met één tik te wijzigen, geen invoerveld; en de knoppenrij voor de taal is met de vingers te bedienen zonder in te zoomen.

## Buiten scope

- **De Jellyfin-kant**: geen bibliotheekscan aanstoten, geen instellingen aanpassen, geen onderzoek naar waarom losse srt's op de share niet opgepikt worden. Wel het noemen waard als gevolg: een AVI die `film.mkv` wordt, verschijnt voor Jellyfin als een nieuw item en verliest daarmee zijn kijkstatus.
- **OCR van beeldondertitels** (PGS, VobSub). Die sporen worden getoond, gemeld en met rust gelaten; bij een terugmux blijven ze gewoon staan.
- **ASS-opmaak behouden bij het bewerken.** De weg daarheen staat beschreven in Beslist in ronde 2 §1, maar wordt nu niet gebouwd.
- **Hercoderen.** Alles is een remux. Een bestand met een codec die niet in Matroska past (zeldzaam) wordt geweigerd, niet omgezet.
- **Een blijvende backup.** Bewust: het origineel wordt vervangen. De bescherming zit in de controle vóór het vervangen, niet in een kopie erna.
- **Meerdere taken tegelijk, of een wachtrij** in de webweg. Eén tegelijk; wie meer wil, gebruikt `convert.py`.
- **Hervatten van een halve mux.** Een afgebroken run laat een brok achter dat weggegooid wordt; er wordt niets halverwege opgepikt.
- **Ondertitels toevoegen die er nog niet zijn** (downloaden, zoeken, automatisch uitlijnen). De tool regelt bij wat er al ligt.
- **De taal uit de tekst van de ondertitel afleiden.** Technisch goed haalbaar — een srt bevat ruim genoeg tekst om Nederlands van Engels te onderscheiden — maar het kost een nieuwe Python-afhankelijkheid, en de belofte in *Keuzes* is dat `requirements.txt` ongewijzigd blijft. De bestandsnaam beslist. Wordt dit later toch nodig (zie *Nog open* §1), dan is de plek een tweede strategie in `lang.detect_srt()`, ná de naam en alleen wanneer die niets oplevert.
- **De bulkrun als systemd-timer.** Bewust nog niet: eerst een paar keer met de hand draaien en de logs lezen. Een `.service`/`.timer`-paar erbij is later tien regels, en het is een slecht idee om iets dat mediabestanden onherroepelijk vervangt vannacht al vanzelf te laten lopen.
- **Ondertitels hernoemen of opruimen op de share.** De tool leest bestandsnamen om er de taal uit af te leiden, maar verbetert ze niet. Een `film.srt` die eigenlijk `film.en.srt` had moeten heten blijft heten zoals hij heet.
