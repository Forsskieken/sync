#!/usr/bin/env python3
"""
convert.py - de bulkweg (blueprint, Opbouw §6).

Twee subcommando's:

    convert.py scan  /mnt/Serie [/mnt/Film ...] [--lang eng]
    convert.py run   /mnt/Serie [/mnt/Film ...] --apply [--drop-srt] [--lang eng]
                                [--limit N] [--min-free-gb 20] [--force]

`scan` telt en toont, wijzigt nooit iets. `run` zonder `--apply` is óók een
proefrun - dat is de belangrijkste veiligheidseigenschap van dit bestand.

Beide commando's lopen door precies dezelfde `mkv._resolve_ingest()` /
`mkv.ingest()` als de webweg (`/api/mkv/ingest`): de taalbepaling, de
overslaan-controle op (taal, forced) en de vervangingsvolgorde staan één keer,
in mkv.py. Dit bestand is uitsluitend bediening: mappen aflopen, een tabel of
een voortgangsregel tonen, tellen, en de vrije-ruimte- en limietbewaking die
alleen in de bulkweg zinvol is.

BELANGRIJK bij het draaien: CACHE_DIR moet gelijk zijn aan de CACHE_DIR van de
draaiende webdienst, anders wijst de systeemwide flock (mkv.py, "Taken") naar
een ander lockbestand en beschermt hij niet meer tegen twee tegelijk lopende
herschrijvingen van dezelfde share. Bron dus hetzelfde env-bestand als de
dienst (bv. `set -a; . ./subtitle-sync.env; set +a`) vóór je dit draait.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import lang
import mkv
from paths import allowed_roots, resolve_in_roots
from subs import find_srts

log = logging.getLogger("convert")


# ---------------------------------------------------------------- logging

def _setup_logging() -> None:
    """Zelfde opzet als app.py (blueprint, Werking bij fouten): naar stderr,
    zodat journald of een omleiding naar een logbestand het opvangt, en het
    niveau alleen op onze eigen modules - de root blijft op WARNING zodat een
    eventuele bibliotheek niet meebabbelt. app.py's _OWN-lijst noemt "convert"
    al met naam, dus deze logger sluit daarbij aan.
    """
    levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    want = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    for name in ("subs", "paths", "lang", "mkv", "convert"):
        logging.getLogger(name).setLevel(want if want in levels else "INFO")
    if want not in levels:
        log.warning("LOG_LEVEL %r is onbekend, INFO gebruikt", want[:20])


# ---------------------------------------------------------------- paden

def _resolve_roots(raw_roots: List[str]) -> List[Path]:
    """Elke opgegeven map door resolve_in_roots (blueprint, §2: "convert.py
    gebruikt resolve_in_roots rechtstreeks"). Dit is geen overbodige controle
    voor een CLI die toch al met de hand bediend wordt: het vangt een
    tikfout in het pad op vóórdat er ook maar één bestand aangeraakt is, en
    het houdt convert.py en de webweg aan dezelfde grens.
    """
    try:
        roots = allowed_roots()
    except ValueError as e:
        print(f"FOUT: ALLOWED_ROOTS is ongeldig: {e}", file=sys.stderr)
        sys.exit(2)

    out = []
    for raw in raw_roots:
        q = resolve_in_roots(raw, roots)
        if q is None:
            print(f"FOUT: {raw} ligt buiten de toegelaten mappen "
                 f"(ALLOWED_ROOTS={[str(r) for r in roots]})", file=sys.stderr)
            sys.exit(2)
        if not q.is_dir():
            print(f"FOUT: {q} is geen map", file=sys.stderr)
            sys.exit(2)
        out.append(q)
    return out


def _iter_videos(root: Path) -> Iterator[Path]:
    """Wandelt root recursief af, op naam gesorteerd, en geeft elk bestand met
    een bekende videoextensie terug (mkv.VIDEO_EXTENSIONS - dezelfde lijst als
    browse() in app.py, blueprint Veiligheid: "alleen bekende extensies").

    Verstopte mappen en bestanden (naam begint met een punt) worden
    overgeslagen - zelfde afspraak als browse(), en het voorkomt en passant
    dat een verweesd `.<naam>.syncpart`-brok (mkv.py, vervangingsvolgorde
    stap 4/5) als video meegeteld wordt: dat matcht toch al geen
    video-extensie, maar zo blijft de regel voor "verstopt" ook overal gelijk.

    followlinks=False (de standaard van os.walk) is bewust: een symlink die
    een map buiten root binnenhaalt mag de wandeling niet meenemen. Elk
    gevonden bestand gaat bovendien nog een keer door resolve_in_roots() in
    _group_videos(), voor het geval een symlink naar een BESTAND (niet een
    map) buiten de toegelaten mappen wijst.
    """
    def _on_walk_error(e: OSError) -> None:
        # Zonder onerror negeert os.walk een onleesbare submap stilzwijgend
        # (keuringsbevinding KLEIN 3): de tabel en de telling lijken dan
        # compleet terwijl er in werkelijkheid een map ontbreekt - een
        # rechtenprobleem dat morgen vanzelf weer weg kan zijn, zonder dat
        # iemand het gemerkt heeft.
        log.warning("map niet leesbaar, overgeslagen: %s (%s)", e.filename, e)

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            f = Path(dirpath) / name
            if f.suffix.lower() in mkv.VIDEO_EXTENSIONS:
                yield f


# ---------------------------------------------------------------- oordeel per bestand

class _StopRun(Exception):
    """Te weinig vrije ruimte: de hele run stopt, geen gewone Python-fout maar
    een signaal om uit de geneste lussen te springen zonder de boekhouding
    (stats) kwijt te raken (blueprint §6: "Stopt bij minder dan --min-free-gb
    vrije ruimte ... in plaats van door te ploeteren tot de schijf vol is")."""


class _LimitReached(Exception):
    """--limit is bereikt. Geen fout - de run stopt netjes, geen mislukte
    bestanden, dus geen afsluitcode 1 hiervoor alleen."""


def _evaluate(video: Path, srt_item: Optional[dict], requested_lang: Optional[str],
             force: bool) -> dict:
    """Eén rij voor de scan-tabel of één stap in run - nooit een tweede
    implementatie van de taalbepaling of de overslaan-controle (blueprint §6):
    dit roept mkv._resolve_ingest() aan, dezelfde functie die mkv.ingest() (en
    dus ook /api/mkv/ingest) gebruikt, en zet de uitkomst om in de kolommen/
    acties uit de blueprinttabel. mkv._resolve_ingest() raakt de schijf niet
    aan buiten een `mkvmerge -J` (identify(), leesalleen), dus dit is voor
    scan en voor een proefrun allebei veilig.

    srt_item is een item uit subs.find_srts() of None (geen losse srt bij
    deze video - blueprint: "overslaan: geen srt").
    """
    container = video.suffix.lower().lstrip(".")
    if srt_item is None:
        return {
            "video": video, "container": container, "srt_name": "-",
            "srt_path": None, "language": "-", "origin": "-", "lang_source": None,
            "outcome": "no-srt", "action_text": "overslaan: geen srt", "plan": None,
            "lang_override": None,
        }

    srt_path = Path(srt_item["path"])
    origin = srt_item["lang_origin"]
    lang_source = srt_item["lang_source"]

    # --lang geldt UITSLUITEND voor bestanden waarvan de bestandsnaam geen
    # taal oplevert (blueprint §6: "een gevonden taal wordt er nooit door
    # overschreven"). _resolve_ingest() zelf kent dat onderscheid niet: zijn
    # `language`-parameter is de expliciete override die de WEBWEG gebruikt
    # wanneer een mens bewust een gevonden taal corrigeert (blueprint §3b, "De
    # voorkant": de taalregel is zelf tikbaar, ook als de taal al uit de naam
    # kwam) - daar moet een override dus wél voorrang krijgen. In de bulkweg
    # is er geen mens die per bestand instemt, dus hier wordt --lang alleen
    # doorgegeven als de naam zelf niets opleverde; anders gaat er `None` in,
    # zodat _resolve_ingest op tag_info["language"] terugvalt.
    override = requested_lang if srt_item["language"] is None else None

    try:
        plan = mkv._resolve_ingest(video, srt_path, override, force)
    except mkv.MkvLanguageUnknown:
        return {
            "video": video, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": "-", "origin": origin,
            "lang_source": lang_source, "outcome": "unknown-lang",
            "action_text": "overslaan: taal onbekend", "plan": None,
            "lang_override": override,
        }
    except mkv.MkvError as e:
        # Alleen een ongeldige --lang-code kan hier nog komen, en die is al
        # één keer vooraf gecontroleerd in main() (lang.normalize(args.lang)) -
        # dit is dus een goedkope, dubbele verdedigingslaag, geen verwachte tak.
        return {
            "video": video, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": "-", "origin": origin,
            "lang_source": lang_source, "outcome": "error",
            "action_text": f"mislukt: {e}", "plan": None,
            "lang_override": override,
        }
    except Exception as e:
        # Keuringsbevinding MATIG 1: _resolve_ingest() draait mkvmerge -J op
        # het bestand (identify()) en dat kan om redenen buiten mkv.MkvError
        # om mislukken - een share die tijdens de scan even wegvalt, een
        # bestand dat tussen het aflopen van de map en dit moment verdwenen
        # is, mkvtoolnix dat niet geïnstalleerd blijkt. Eén onverwacht kapot
        # bestand mag de hele scan/run niet met een traceback laten crashen
        # (blueprint, Werking bij fouten): dit bestand telt als mislukt, de
        # rest van de run gaat door. log.exception() zet de traceback in het
        # journaal, niet op het scherm - dat blijft de nette tabel/regel.
        log.exception("onverwachte fout bij het beoordelen van %s + %s",
                     video, srt_item["name"])
        return {
            "video": video, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": "-", "origin": origin,
            "lang_source": lang_source, "outcome": "error",
            "action_text": f"mislukt: onverwachte fout ({e}) - zie journaal", "plan": None,
            "lang_override": override,
        }

    lang_code = plan["label"]["language"]

    if plan["skip"] is not None:
        suffix = "+forced" if plan["label"]["forced"] else ""
        return {
            "video": video, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": lang_code, "origin": origin,
            "lang_source": lang_source, "outcome": "skip-existing",
            "action_text": f"overslaan: heeft al {lang_code}{suffix}", "plan": plan,
            "lang_override": override,
        }

    if plan["is_rename"] and plan["target"].exists():
        # Stap 9 van de vervangingsvolgorde weigert dit ook nog een keer op
        # het moment zelf (mkv.py, _execute_transaction) - deze controle hier
        # is uitsluitend voor de preview, dezelfde soort goedkope,
        # niet-business-regel verdedigingslaag als _video_path()/mkv_ingest()
        # in app.py al hebben (blueprint §4: "extra ... verdedigingslaag").
        return {
            "video": video, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": lang_code, "origin": origin,
            "lang_source": lang_source, "outcome": "refuse",
            "action_text": f"weigeren: {plan['target'].name} bestaat al", "plan": plan,
            "lang_override": override,
        }

    verb = "omzetten" if plan["is_rename"] else "inbouwen"
    return {
        "video": video, "container": container, "srt_name": srt_item["name"],
        "srt_path": srt_path, "language": lang_code, "origin": origin,
        "lang_source": lang_source, "outcome": "apply",
        "action_text": f"{verb} ({lang_code})", "plan": plan,
        "lang_override": override,
    }


def _group_videos(roots: List[Path]) -> Iterator[Tuple[Path, List[dict]]]:
    """Alle video's onder de gegeven roots, op naam gesorteerd, elk met de
    (mogelijk lege) lijst losse srt's die erbij horen. Gebruikt door scan én
    run, zodat beide exact dezelfde bestanden in dezelfde volgorde zien.

    Gegroepeerd per video (in plaats van platte (video, srt)-paren) zodat de
    aanroeper de srt's van dezelfde video na elkaar kan afhandelen en aan
    elkaar kan doorgeven wat een eerdere srt in de groep al zou opleveren
    (keuringsbevinding MATIG 2, zie _evaluate_group()/_run_group_apply()).
    """
    # Eén keer gelezen in plaats van per bestand: ALLOWED_ROOTS verandert niet
    # tijdens een run, en JSON opnieuw parsen voor elke video is pure winst
    # zonder nut op een collectie van duizenden bestanden.
    allowed = allowed_roots()
    for root in roots:
        for video in _iter_videos(root):
            # Nog een keer door resolve_in_roots (zie _iter_videos): een
            # symlink naar een bestand buiten de toegelaten mappen mag hier
            # niet doorglippen, ook al matchte hij de videoextensie-test.
            if resolve_in_roots(str(video), allowed) is None:
                log.warning("bestand geweigerd, ligt buiten ALLOWED_ROOTS ondanks "
                           "dat het onder een gescande map stond (symlink?): %s", video)
                continue
            yield video, find_srts(video)


def _group_baseline_tracks(video: Path) -> Optional[List[Tuple[str, bool]]]:
    """Bestaande tekstsporen van `video` vóór deze run, als (taal, forced)-
    paren - dezelfde selectie als mkv._resolve_ingest() voor zijn eigen
    skip-controle gebruikt (S_HDMV/PGS en S_VOBSUB tellen niet mee).

    Alleen nodig wanneer een video meer dan één losse srt heeft
    (keuringsbevinding MATIG 2): de eerste srt van zo'n groep gaat gewoon via
    _evaluate()/_resolve_ingest(), die doet zijn eigen identify() toch al,
    maar om de TWEEDE en volgende srt goed te kunnen voorspellen moet ook
    bekend zijn wat er al in het bestand zat vóórdat deze run begon - vandaar
    hier één losse, expliciete identify()-aanroep.

    Geeft None bij een leesfout (bijvoorbeeld een beschadigd bestand): de
    aanroeper valt dan terug op de ongeketende afhandeling per srt, met een
    WARN erbij, in plaats van te gissen naar wat er al in zit.
    """
    try:
        info = mkv.identify(video)
    except mkv.MkvError as e:
        log.warning("kon bestaande sporen van %s niet bepalen om de srt's van "
                   "deze video in dezelfde run op elkaar af te stemmen: %s", video, e)
        return None
    return [(t["language"], bool(t["forced"])) for t in info["tracks"]
           if t["type"] == "subtitles" and t["editable"] is not False]


def _evaluate_chained(target: Path, existing: List[Tuple[str, bool]], srt_item: dict,
                      requested_lang: Optional[str], force: bool) -> dict:
    """Zusje van _evaluate(), voor de tweede en volgende srt binnen één groep
    (keuringsbevinding MATIG 2) nadat de eerste al een (voorspelde) omzetting
    of inbouw opleverde. Roept BEWUST geen mkv._resolve_ingest()/identify()
    aan: `target` bestaat op dit moment mogelijk nog helemaal niet op schijf -
    scan en een proefrun voeren nooit iets uit - dus een identify() erop zou
    een leesfout geven in plaats van een voorspelling.

    In plaats daarvan dupliceert dit het staartje van _resolve_ingest() dat
    GEEN bestandstoegang nodig heeft (skip-controle, default-vlag, label). De
    taalbepaling zelf wordt niet opnieuw gedaan: srt_item komt al met het
    kant-en-klare oordeel van find_srts()/lang.detect_srt(), dezelfde bron als
    _resolve_ingest zelf gebruikt.

    Puur informatief: bij --apply gebruikt cmd_run() deze functie NIET voor de
    echte uitvoering (zie _run_group_apply) - mkv.ingest() doet daar zijn
    eigen, echte identify() op het dan al bestaande doelbestand. Een fout in
    deze voorspelling kan dus nooit tot een verkeerd geschreven bestand leiden,
    hoogstens tot een tabel die niet klopt met wat --apply zou doen.
    """
    container = target.suffix.lower().lstrip(".")
    srt_path = Path(srt_item["path"])
    origin = srt_item["lang_origin"]
    lang_source = srt_item["lang_source"]
    override = requested_lang if srt_item["language"] is None else None

    if override is not None:
        lang_code = lang.normalize(override)
        if lang_code is None:
            return {
                "video": target, "container": container, "srt_name": srt_item["name"],
                "srt_path": srt_path, "language": "-", "origin": origin,
                "lang_source": lang_source, "outcome": "error",
                "action_text": f"mislukt: onbekende taalcode aangeleverd "
                              f"(lengte {len(str(override))})",
                "plan": None, "lang_override": override,
            }
    elif srt_item["language"]:
        lang_code = srt_item["language"]
    else:
        log.info("taal onbepaald (%s, %s)", srt_item["name"], origin)
        return {
            "video": target, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": "-", "origin": origin,
            "lang_source": lang_source, "outcome": "unknown-lang",
            "action_text": "overslaan: taal onbekend", "plan": None,
            "lang_override": override,
        }

    forced = bool(srt_item["forced"])
    hearing_impaired = bool(srt_item["hearing_impaired"])

    skip = None
    if not force:
        for ex_lang, ex_forced in existing:
            if mkv._same_sub_key(ex_lang, ex_forced, lang_code, forced):
                skip = {
                    "path": str(target), "language": lang_code, "forced": forced,
                    "reason": f"{target.name} heeft al een tekstspoor met taal "
                             f"{lang_code}{' (forced)' if forced else ''} (van "
                             f"vóór deze run, of uit een eerdere srt in dezelfde run)",
                }
                break

    # Zelfde regel als _resolve_ingest (Beslist in ronde 2 §3): default aan
    # zolang er nog geen ander tekstspoor is en nooit bij forced. "existing"
    # is hier al gegroeid met wat eerdere srt's in deze groep zouden
    # toevoegen, dus zodra die niet leeg is heeft deze srt de default-vlag
    # sowieso al niet meer nodig.
    label = {
        "language": lang_code, "track_name": lang.display(lang_code),
        "default": (not existing) and not forced, "forced": forced,
        "hearing_impaired": hearing_impaired,
    }
    # is_rename staat vast op False: een gekende srt in een groep komt per
    # definitie NA een omzetting/inbouw die het doel al naar een .mkv gebracht
    # heeft (of het was al een .mkv) - dit is dus altijd een inbouw, nooit
    # nog een keer een omzetting (blueprint keuringsbevinding MATIG 2: "de
    # tweede srt wordt een inbouw in de nieuwe MKV, geen omzetting").
    plan = {"label": label, "target": target, "is_rename": False, "skip": skip,
           "tag_info": None}

    if skip is not None:
        suffix = "+forced" if forced else ""
        return {
            "video": target, "container": container, "srt_name": srt_item["name"],
            "srt_path": srt_path, "language": lang_code, "origin": origin,
            "lang_source": lang_source, "outcome": "skip-existing",
            "action_text": f"overslaan: heeft al {lang_code}{suffix}", "plan": plan,
            "lang_override": override,
        }

    return {
        "video": target, "container": container, "srt_name": srt_item["name"],
        "srt_path": srt_path, "language": lang_code, "origin": origin,
        "lang_source": lang_source, "outcome": "apply",
        "action_text": f"inbouwen ({lang_code})", "plan": plan,
        "lang_override": override,
    }


def _evaluate_group(video: Path, srt_items: List[dict], requested_lang: Optional[str],
                    force: bool) -> List[dict]:
    """Alle rijen voor één video in één keer, met de srt's in dezelfde groep
    op elkaar afgestemd (keuringsbevinding MATIG 2): een video met N losse
    srt's levert in dezelfde run N (voorspelde) tekstsporen op, niet telkens
    hetzelfde ene spoor - zonder deze doorgifte zou de tweede srt nog tegen de
    situatie VAN VÓÓR de eerste srt geëvalueerd worden, en bij een AVI/MP4
    (waar de eerste srt een omzetting is) zou dat een pad zijn dat na een
    echte --apply-run niet meer bestaat.

    Puur een voorspelling, geen enkele schrijfactie: gebruikt door scan() en
    door run() zónder --apply. run() MÉT --apply doet zijn eigen, op de echte
    uitkomst gebaseerde ketening in _run_group_apply() - één identify() op een
    zojuist echt aangemaakt bestand is betrouwbaarder dan een voorspelling
    ervan, en dat is precies waarom scan/proefrun en --apply hier twee
    (verwant, maar niet identieke) code-paden hebben.
    """
    if not srt_items:
        return [_evaluate(video, None, requested_lang, force)]

    rows = [_evaluate(video, srt_items[0], requested_lang, force)]
    if len(srt_items) == 1:
        return rows

    baseline = _group_baseline_tracks(video)
    if baseline is None:
        # Kon niet bepalen wat er al in zit (zie _group_baseline_tracks) - elke
        # volgende srt gewoon los evalueren tegen het ongewijzigde origineel is
        # veiliger dan gissen. De actiekolom van zo'n latere rij kan daardoor
        # een tekstspoor voorspellen dat er bij --apply niet meer bij komt (de
        # echte identify() daar ziet het wél goed) - vandaar de WARN hierboven.
        rows.extend(_evaluate(video, s, requested_lang, force) for s in srt_items[1:])
        return rows

    target: Optional[Path] = None
    tracks: List[Tuple[str, bool]] = list(baseline)
    if rows[0]["outcome"] == "apply":
        first_plan = rows[0]["plan"]
        target = first_plan["target"]
        tracks.append((first_plan["label"]["language"], first_plan["label"]["forced"]))

    for srt_item in srt_items[1:]:
        if target is None:
            # De eerste srt in deze groep werd niet toegepast (overgeslagen,
            # geweigerd of mislukt) - er is dus niets veranderd om aan de
            # volgende srt door te geven; die evalueert gewoon tegen het
            # origineel, zoals zonder groepering ook zou gebeuren.
            row = _evaluate(video, srt_item, requested_lang, force)
        else:
            row = _evaluate_chained(target, tracks, srt_item, requested_lang, force)
        rows.append(row)
        if row["outcome"] == "apply":
            plan = row["plan"]
            if target is None:
                target = plan["target"]
            tracks.append((plan["label"]["language"], plan["label"]["forced"]))

    return rows


# ---------------------------------------------------------------- scan

def _format_table(rows: List[dict]) -> str:
    """Eenvoudige, dependency-vrije tabel (blueprint: geen nieuwe
    afhankelijkheden). Padbreedte per kolom is aan een plafond gebonden: het
    pad zelf kan wild uiteenlopen in lengte (map- versus bestandsdiepte), en
    zonder plafond zou één lang pad alle andere regels een halve terminal
    breed lege ruimte geven. ljust() KORT nooit af (het plakt alleen bij als
    de tekst korter is dan de breedte), dus er gaat geen inhoud verloren -
    een enkele uitschieter misaligneert hoogstens zijn eigen regel.
    """
    headers = ("pad", "container", "gevonden srt", "taal", "herkomst", "actie")
    caps = (80, 10, 40, 6, 45, 30)
    body = [(str(r["video"]), r["container"], r["srt_name"], r["language"],
            r["origin"], r["action_text"]) for r in rows]

    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = min(caps[i], max(widths[i], len(cell)))

    def fmt(cells: Tuple[str, ...]) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(headers), fmt(tuple("-" * w for w in widths))]
    lines.extend(fmt(row) for row in body)
    return "\n".join(lines)


def cmd_scan(args: argparse.Namespace) -> None:
    roots = _resolve_roots(args.roots)
    if args.lang is not None and lang.normalize(args.lang) is None:
        print(f"FOUT: onbekende taalcode --lang {args.lang!r}", file=sys.stderr)
        sys.exit(2)

    # Geen lijstcomprehensie meer (keuringsbevinding MATIG 1): die had geen
    # foutafhandeling, dus één onverwacht kapot bestand liet de hele scan met
    # een traceback crashen - geen tabel, geen samenvatting. Nu telt zo'n
    # bestand als een foutrij en de scan van de rest gaat door.
    rows: List[dict] = []
    video_count = 0
    for video, srt_items in _group_videos(roots):
        video_count += 1
        try:
            rows.extend(_evaluate_group(video, srt_items, args.lang, force=False))
        except Exception as e:
            log.exception("onverwachte fout bij het beoordelen van %s", video)
            rows.append({
                "video": video, "container": video.suffix.lower().lstrip("."),
                "srt_name": "-", "srt_path": None, "language": "-", "origin": "-",
                "lang_source": None, "outcome": "error",
                "action_text": f"mislukt: onverwachte fout ({e}) - zie journaal",
                "plan": None, "lang_override": None,
            })

    if not rows:
        print(f"Geen video's gevonden onder {', '.join(str(r) for r in roots)}.")
        return

    print(_format_table(rows))

    # Open punt 3 uit de blueprint: "Hoeveel bestanden hebben een kale
    # film.srt zonder taalaanduiding? Daar hangt aan of de bulkweg bruikbaar
    # is." Dit getal moet in één regel te zien zijn, niet pas na het tellen
    # van de tabel met de hand.
    #
    # video_count komt uit de groepering hierboven, niet uit een dedup op
    # r["video"] (keuringsbevinding MATIG 2): een geketende rij toont daar
    # bewust het (voorspelde) NIEUWE pad van de video, dus tellen op die
    # kolom zou dezelfde video als film.avi én als film.mkv meetellen.
    srt_rows = [r for r in rows if r["outcome"] != "no-srt"]
    none_n = sum(1 for r in srt_rows if r["lang_source"] == "none")
    unmatched_n = sum(1 for r in srt_rows if r["lang_source"] == "unmatched")
    ambiguous_n = sum(1 for r in srt_rows if r["lang_source"] == "ambiguous")
    m = len(srt_rows)

    print()
    print(f"{video_count} video's, {m} bijhorende srt-bestanden gevonden onder "
         f"{', '.join(str(r) for r in roots)}.")
    if m:
        print(f"{none_n} van de {m} srt-bestanden hebben geen taalaanduiding "
             f"(een kale \"film.srt\").")
        if unmatched_n or ambiguous_n:
            print(f"Daarnaast: {unmatched_n} met een onherkend label, {ambiguous_n} "
                 f"met twee talen in de naam - die slaan de bulkweg net zo goed over "
                 f"zonder --lang. Samen {none_n + unmatched_n + ambiguous_n} van de {m} "
                 f"({100 * (none_n + unmatched_n + ambiguous_n) / m:.0f}%) zonder taal "
                 f"uit de naam.")
    if args.lang:
        print(f"(met --lang {args.lang}: bovenstaande tabel toont het effect al - "
             f"alleen de rijen zonder taal uit de naam zijn aangepast)")


# ---------------------------------------------------------------- run

class _Stats:
    def __init__(self) -> None:
        self.converted = 0
        self.ingested = 0
        self.skipped = 0
        self.skipped_unknown = 0
        self.failed = 0
        self.applied = 0            # aantal ECHTE ingest()-aanroepen (voor --limit)
        self.bytes_total = 0


def _row_line(row: dict) -> str:
    """Eén grep-bare regel per bestand voor de voortgang tijdens run - dezelfde
    velden als de scan-tabel, maar als lopende regel in plaats van een tabel
    die pas na afloop compleet is (run kan uren duren over een grote
    collectie, blueprint Keuzes: "tweehonderd bestanden van 5 GB is uren werk").
    """
    srt = f" + {row['srt_name']}" if row["srt_path"] is not None else ""
    return f"{row['video']}{srt} [{row['language']}, {row['origin']}] -> {row['action_text']}"


def _handle_row(row: dict, args: argparse.Namespace, cache_dir: Path,
                stats: _Stats) -> Optional[Path]:
    """Verwerkt één (video, srt)-paar: telt mee, en bij --apply wordt het ook
    echt uitgevoerd. Gooit _StopRun (te weinig ruimte) of _LimitReached
    (--limit bereikt) om de buitenste lus in cmd_run() te laten stoppen.

    Geeft het NIEUWE, echte pad terug zodra dit bestand bij --apply
    daadwerkelijk (en niet als skip) omgezet/ingebouwd is - anders None.
    _run_group_apply() gebruikt dat om de volgende srt van dezelfde
    oorspronkelijke video tegen het juiste, inmiddels bestaande bestand te
    evalueren (keuringsbevinding MATIG 2).
    """
    outcome = row["outcome"]

    # De limiet controleren VÓÓR het printen van deze regel (keuringsronde:
    # zonder deze volgorde zou het bestand dat de limiet net overschrijdt nog
    # als "omzetten"/"inbouwen" op het scherm verschijnen, terwijl het
    # helemaal niet aangepakt wordt - verwarrend bij het teruglezen van een
    # lange run). Geldt alleen voor het pad dat ook echt iets zou uitvoeren.
    if outcome == "apply" and args.apply and args.limit is not None \
            and stats.applied >= args.limit:
        raise _LimitReached(f"limiet van {args.limit} bestand(en) bereikt")

    line = _row_line(row)
    print(line)
    log.info(line)

    if outcome == "no-srt":
        stats.skipped += 1
        return None
    if outcome == "unknown-lang":
        stats.skipped += 1
        stats.skipped_unknown += 1
        return None
    if outcome == "skip-existing":
        stats.skipped += 1
        return None
    if outcome in ("refuse", "error"):
        stats.failed += 1
        log.error("%s: %s", row["video"], row["action_text"])
        return None

    # outcome == "apply": dit bestand zou omgezet/ingebouwd worden.
    is_convert = row["plan"]["is_rename"]

    if not args.apply:
        # Proefrun: alleen tellen wat er ZOU gebeuren, geen enkele aanraking
        # van de schijf (blueprint §6: "run zonder --apply is een proefrun en
        # raakt niets aan - dat is de belangrijkste veiligheidseigenschap").
        if is_convert:
            stats.converted += 1
        else:
            stats.ingested += 1
        return None

    min_free = int(args.min_free_gb * 1024 ** 3)
    try:
        free = shutil.disk_usage(row["video"].parent).free
    except OSError as e:
        # Keuringsbevinding MATIG 1: een kale OSError hier (share tijdelijk
        # weg, map net verdwenen) mocht voorheen ongedekt naar buiten
        # bubbelen en de hele run met een traceback laten crashen. Dit ene
        # bestand telt nu als mislukt, de run gaat door met het volgende -
        # net als bij elke andere mislukking in deze functie.
        stats.failed += 1
        log.error("kon vrije ruimte niet bepalen voor %s, bestand overgeslagen: %s",
                  row["video"], e)
        return None
    if free < min_free:
        raise _StopRun(
            f"te weinig vrije ruimte in {row['video'].parent}: "
            f"{free / 1_073_741_824:.1f} GB vrij, --min-free-gb {args.min_free_gb} nodig")

    # Dezelfde grendel als submit_ingest() (blueprint §6/§3, "Taken"):
    # systeemwide via flock op CACHE_DIR/mkv.lock, zodat een tegelijk
    # draaiende webdienst nooit hetzelfde bestand tegelijk herschrijft.
    # ingest() zelf grendelt niet - dat doet submit_ingest() voor de webweg -
    # dus convert.py moet dat hier zelf doen, met dezelfde primitief.
    file_lock = mkv._FileLock(cache_dir)
    try:
        file_lock.acquire(str(row["video"]))
    except mkv.MkvBusy as e:
        stats.failed += 1
        log.error("%s: kon niet starten, %s", row["video"], e)
        return None
    try:
        # row["lang_override"] is NIET args.lang: die geldt alleen als de
        # bestandsnaam zelf geen taal opleverde (zie _evaluate()) - een
        # gevonden taal mag --lang nooit overschrijven.
        result = mkv.ingest(row["video"], row["srt_path"], cache_dir,
                            language=row["lang_override"], drop_srt=args.drop_srt,
                            force=args.force)
    except mkv.MkvError as e:
        stats.failed += 1
        log.error("ingest mislukt op %s: %s", row["video"], e)
        return None
    except Exception as e:
        # Keuringsbevinding MATIG 1: dezelfde brede vangnet als
        # mkv._run_ingest_job() al heeft voor de webweg. mkv.ingest() zelf
        # vangt alleen zijn eigen MkvError af; een bug of een onverwachte
        # OS-fout tijdens het muxen mag de hele bulkrun niet met een
        # traceback laten crashen - dit ene bestand telt als mislukt en de
        # run gaat door met het volgende. log.exception() zet de volledige
        # traceback in het journaal.
        stats.failed += 1
        log.exception("onverwachte fout tijdens ingest op %s", row["video"])
        return None
    finally:
        file_lock.release()

    if result.get("skipped"):
        # Keuringsbevinding KLEIN 4: mkv.ingest() doet zijn eigen, latere
        # _resolve_ingest()-aanroep en kan dus tot een andere uitkomst komen
        # dan het plan van _evaluate() hierboven - bijvoorbeeld omdat de
        # webdienst dit bestand tussen scan/plan en deze uitvoering al van
        # hetzelfde spoor voorzien heeft. Dat telt als overgeslagen, niet als
        # toegepast; stats.applied (waar --limit op telt) hoort dan ook niet
        # opgehoogd te zijn - vandaar dat die ophoging hieronder staat en niet
        # vooraf.
        stats.skipped += 1
        log.info("%s: %s", row["video"], result.get("reason", "overgeslagen"))
        return None

    stats.applied += 1
    if is_convert:
        stats.converted += 1
    else:
        stats.ingested += 1
    try:
        stats.bytes_total += Path(result["path"]).stat().st_size
    except OSError as e:
        # Puur voor de GB-teller in de slotregel; het bestand is al klaar en
        # correct - deze mislukking mag de uitkomst niet alsnog als "mislukt"
        # tellen.
        log.debug("kon grootte van %s niet lezen voor de samenvatting: %s",
                 result.get("path"), e)

    return Path(result["path"])


def _run_group_apply(video: Path, srt_items: List[dict], args: argparse.Namespace,
                     cache_dir: Path, stats: _Stats) -> None:
    """Verwerkt één video-groep bij --apply, met paddoorgifte tussen de srt's
    van dezelfde oorspronkelijke video (keuringsbevinding MATIG 2): na een
    geslaagde omzetting bestaat het oorspronkelijke pad niet meer (stap 9 van
    de vervangingsvolgorde verwijdert het bronbestand), dus de tweede en
    volgende srt moeten tegen het NIEUWE pad geëvalueerd worden.

    Dit gebruikt de ECHTE uitkomst van _handle_row() (het pad dat het
    teruggeeft), geen voorspelling: zodra een --apply-run daadwerkelijk heeft
    plaatsgevonden bestaat het doel echt, en een gewone _evaluate()/
    identify()-aanroep daarop is betrouwbaarder dan raden - vandaar dat dit
    NIET dezelfde weg volgt als _evaluate_group() (scan/proefrun).
    """
    if not srt_items:
        row = _evaluate(video, None, args.lang, args.force)
        _handle_row(row, args, cache_dir, stats)
        return

    current = video
    for srt_item in srt_items:
        try:
            row = _evaluate(current, srt_item, args.lang, args.force)
            new_path = _handle_row(row, args, cache_dir, stats)
        except (_StopRun, _LimitReached, KeyboardInterrupt):
            raise
        except Exception as e:
            # Keuringsbevinding MATIG 1, per-item vangnet ook binnen een
            # groep: een onverwachte fout op de tweede srt van een video mag
            # de derde srt van dezelfde video niet ook nog meeslepen.
            stats.failed += 1
            log.exception("onverwachte fout bij %s + %s, bestand overgeslagen",
                         current, srt_item["name"])
            print(f"FOUT: {current} + {srt_item['name']}: onverwachte fout, "
                 f"overgeslagen - zie journaal", file=sys.stderr)
            continue
        if new_path is not None:
            current = new_path


def _notify_ha(summary: str, ok: bool) -> None:
    """Stuurt de slotregel als POST naar Home Assistant, als HA_WEBHOOK_URL
    gezet is (blueprint §6). Staat hij er niet, dan gebeurt er niets en is dat
    geen fout - dit is een optionele melding, geen vereiste.

    Het tweede geheim (blueprint, Veiligheid): alleen uit de omgeving, nooit
    in een logregel. Bij het loggen wordt de URL ingekort tot schema en
    hostnaam, dus een eventueel pad/token in de URL zelf komt nooit in het
    journaal terecht. Geen nieuwe afhankelijkheid (blueprint, Keuzes):
    urllib uit de standaardbibliotheek volstaat voor één simpele POST.
    """
    url = os.environ.get("HA_WEBHOOK_URL", "").strip()
    if not url:
        return
    parts = urlsplit(url)
    short = f"{parts.scheme}://{parts.hostname}" if parts.hostname else "(onleesbare url)"
    body = json.dumps({"summary": summary, "ok": ok}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("melding naar HA (%s) verstuurd, status %s", short, r.status)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # De run zelf is al voorbij op dit punt - een mislukte melding mag de
        # afsluitcode niet meer beïnvloeden, dat zou de echte uitkomst
        # (mislukte bestanden, of niet) verbergen achter een heel ander soort
        # fout (een onbereikbare Home Assistant).
        log.error("melding naar HA (%s) mislukt: %s", short, e)


def cmd_run(args: argparse.Namespace) -> None:
    roots = _resolve_roots(args.roots)
    if args.lang is not None and lang.normalize(args.lang) is None:
        print(f"FOUT: onbekende taalcode --lang {args.lang!r}", file=sys.stderr)
        sys.exit(2)

    cache_dir = Path(os.environ.get("CACHE_DIR", "/tmp/subtitle-sync"))
    log.info("CACHE_DIR=%s (moet gelijk zijn aan de draaiende webdienst, anders "
             "beschermt de grendel niet)", cache_dir)

    if not args.apply:
        print("PROEFRUN - er wordt niets geschreven. Voeg --apply toe om het echt te doen.\n")

    stats = _Stats()
    t0 = time.monotonic()
    stopped: Optional[str] = None
    interrupted = False
    try:
        for video, srt_items in _group_videos(roots):
            if args.apply:
                # Interleaved: elke srt van deze video wordt geëvalueerd EN
                # meteen uitgevoerd voor de volgende aan de beurt is, zodat
                # een geslaagde omzetting/inbouw haar echte, nieuwe pad kan
                # doorgeven aan de rest van de groep (keuringsbevinding
                # MATIG 2). _run_group_apply() heeft zijn eigen per-item
                # vangnet, dus hier alleen de regeluitzonderingen doorlaten.
                try:
                    _run_group_apply(video, srt_items, args, cache_dir, stats)
                except (_StopRun, _LimitReached, KeyboardInterrupt):
                    raise
                except Exception:
                    # Zou niet moeten gebeuren (_run_group_apply vangt zelf al
                    # per item af) - laatste vangnet zodat een gemiste tak
                    # daar nog steeds niet de hele run laat crashen.
                    stats.failed += 1
                    log.exception("onverwachte fout bij de groep rond %s", video)
            else:
                # Proefrun: geen enkele schrijfactie, dus veilig om de hele
                # groep in één keer te voorspellen (keuringsbevinding MATIG 2)
                # - dezelfde weg als scan(), zodat beide exact hetzelfde
                # voorspellen.
                try:
                    rows = _evaluate_group(video, srt_items, args.lang, args.force)
                except (_StopRun, _LimitReached, KeyboardInterrupt):
                    raise
                except Exception:
                    stats.failed += 1
                    log.exception("onverwachte fout bij het beoordelen van %s", video)
                    continue
                for row in rows:
                    _handle_row(row, args, cache_dir, stats)
    except _LimitReached as e:
        print(f"\n{e}, run gestopt.")
        log.info("run gestopt: %s", e)
    except _StopRun as e:
        print(f"\nFOUT: {e} - run gestopt.", file=sys.stderr)
        log.error("run gestopt: %s", e)
        stopped = str(e)
    except KeyboardInterrupt:
        # Blueprint §6: "Wordt de run met Ctrl-C ... afgebroken, dan is het
        # bestand dat bezig was onaangeroerd (het brok wordt in finally
        # opgeruimd) ... opnieuw starten pikt vanzelf op." Dat opruimen zit al
        # in mkv.py (_execute_transaction, stap 10) en in file_lock.release()
        # hierboven (beide gewone finally-blokken, die ook bij een
        # KeyboardInterrupt lopen) - hier hoeft dus alleen de samenvatting
        # nog netjes getoond te worden in plaats van een kale traceback.
        print("\nOnderbroken (Ctrl-C). Het bestand dat bezig was is onaangeroerd; "
             "opnieuw starten gaat vanzelf verder waar dit stopte.", file=sys.stderr)
        log.warning("run onderbroken met Ctrl-C na %d bestand(en)", stats.applied)
        interrupted = True
    except Exception as e:
        # Keuringsbevinding MATIG 1: elke stap hierboven heeft nu een eigen
        # vangnet, maar dit is de laatste linie - iets in _group_videos() zelf
        # (bijv. allowed_roots() of find_srts() die alsnog een onverwachte
        # fout geeft) mag de run niet stil laten crashen ZONDER samenvatting
        # en ZONDER HA-melding. "Stilte mag nooit 'waarschijnlijk goed'
        # betekenen" (blueprint, Werking bij fouten) - dus ook hier: de
        # samenvatting hieronder blijft draaien, met een duidelijke reden
        # waarom de run vroegtijdig stopte.
        print(f"\nFOUT: onverwachte fout - run gestopt: {e}", file=sys.stderr)
        log.exception("onverwachte fout, run gestopt")
        stopped = f"onverwachte fout: {e}"

    elapsed_min = (time.monotonic() - t0) / 60
    gb = stats.bytes_total / 1_073_741_824
    summary = (f"{stats.converted} omgezet, {stats.ingested} ingebouwd, "
              f"{stats.skipped} overgeslagen (waarvan {stats.skipped_unknown} taal "
              f"onbekend), {stats.failed} mislukt, totaal {gb:.1f} GB, "
              f"{elapsed_min:.1f} min")
    if stopped:
        summary += f" - VROEGTIJDIG GESTOPT: {stopped}"
    if interrupted:
        summary += " - onderbroken met Ctrl-C"
    print("\n" + summary)
    log.info(summary)

    _notify_ha(summary, ok=(stats.failed == 0 and stopped is None and not interrupted))

    if interrupted:
        sys.exit(130)  # gangbare afsluitcode voor SIGINT
    if stats.failed > 0 or stopped is not None:
        sys.exit(1)


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="convert.py",
        description=(
            "Bulkomzetting van losse .srt-bestanden naar ingebedde MKV-sporen "
            "(blueprint docs/BLUEPRINT.md, Opbouw §6). Gebruikt dezelfde "
            "mkv.ingest() als de webpagina - alleen de bediening verschilt: "
            "deze tool werkt een hele collectie na elkaar af."
        ),
        epilog=(
            "Begin altijd met 'scan' en lees de taal- en herkomstkolom na. "
            "Draai daarna 'run' ZONDER --apply (de standaard) en pas als dat "
            "klopt 'run --apply'. CACHE_DIR en ALLOWED_ROOTS komen uit de "
            "omgeving - bron hetzelfde .env-bestand als de webdienst, anders "
            "beschermt de grendel niet tegen een gelijktijdige webhandeling."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser(
        "scan", help="Toon een tabel met wat er zou gebeuren; wijzigt niets")
    p_scan.add_argument("roots", nargs="+", metavar="MAP",
                        help="Eén of meer mappen om (recursief) te doorzoeken")
    p_scan.add_argument("--lang", metavar="CODE",
                        help="Taal voor bestanden waarvan de naam geen taal oplevert "
                             "- overschrijft nooit een taal die wél gevonden is")
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser(
        "run", help="Voer de omzetting uit - standaard een proefrun, zie --apply")
    p_run.add_argument("roots", nargs="+", metavar="MAP",
                       help="Eén of meer mappen om (recursief) te doorzoeken")
    p_run.add_argument("--apply", action="store_true",
                       help="Schrijf ook echt. Zonder deze vlag verandert er niets op schijf")
    p_run.add_argument("--drop-srt", action="store_true",
                       help="Verwijder de losse .srt na een geslaagde inbouw "
                            "(standaard: laten staan)")
    p_run.add_argument("--lang", metavar="CODE",
                       help="Taal voor bestanden waarvan de naam geen taal oplevert "
                            "- overschrijft nooit een taal die wél gevonden is")
    p_run.add_argument("--limit", type=int, metavar="N",
                       help="Stop na N bestanden die echt omgezet/ingebouwd zijn")
    p_run.add_argument("--min-free-gb", type=float, default=20.0, metavar="GB",
                       help="Stop de run zodra de vrije ruimte hieronder komt (standaard 20)")
    p_run.add_argument("--force", action="store_true",
                       help="Zet de overslaan-controle op (taal, forced) uit")
    p_run.set_defaults(func=cmd_run)
    return p


def main(argv: Optional[List[str]] = None) -> None:
    _setup_logging()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
