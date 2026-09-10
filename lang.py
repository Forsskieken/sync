#!/usr/bin/env python3
"""
lang.py - taalcodes normaliseren en de taal uit een bestandsnaam halen.

Zuivere tabelcode: geen bestandstoegang, geen netwerk, geen afhankelijkheden,
en geen import uit dit project. Daardoor kan iedereen hem importeren
(subs.find_srts, mkv.ingest, app.py, convert.py) zonder kringimport.

De uitkomst is altijd een ISO 639-2/T-code van drie letters, want het oude
Language-element in Matroska kan niets anders aan en elke mkvmerge-versie
aanvaardt het. "nld" dus, niet "dut" - en omdat alles door normalize() gaat is
het verschil onschadelijk: een bestaand "dut"-spoor en een nieuwe ".nl.srt"
zijn daarna aantoonbaar dezelfde taal.

Zelftest: python3 lang.py --selftest
"""

import re
import sys
from typing import Dict, List, Optional

# --------------------------------------------------------------- de tabel

# Per taal vier ingangen: ISO 639-1 (twee letters), ISO 639-2/T, ISO 639-2/B en
# de naam in het Engels of het Nederlands. De tabel hoeft niet volledig te zijn:
# een ontbrekende taal kost één regel erbij, een te gulle tabel kost een
# verkeerd label - en een verkeerd label zet Jellyfin op het verkeerde spoor.
#
# De twee-lettercode staat er alleen bij de talen die hier realistisch
# voorkomen. Voor de exotische B/T-paren (cze, gre, ice, ...) is die bewust
# weggelaten: codes als "is", "my" en "bo" zijn ook gewone woorden en zouden in
# een bestandsnaam vaker als taal gelezen worden dan bedoeld.
_TABLE = [
    # (T-code, ISO 639-1, B-code, weergavenaam, namen)
    ("nld", "nl", "dut", "Nederlands", ("dutch", "nederlands", "flemish", "vlaams")),
    ("eng", "en", None, "Engels", ("english", "engels")),
    ("fra", "fr", "fre", "Frans", ("french", "frans")),
    ("deu", "de", "ger", "Duits", ("german", "duits", "deutsch")),
    ("spa", "es", None, "Spaans", ("spanish", "spaans", "espanol")),
    ("ita", "it", None, "Italiaans", ("italian", "italiaans")),
    ("dan", "da", None, "Deens", ("danish", "deens")),
    ("swe", "sv", None, "Zweeds", ("swedish", "zweeds")),
    ("nor", "no", None, "Noors", ("norwegian", "noors")),
    ("fin", "fi", None, "Fins", ("finnish", "fins")),
    ("por", "pt", None, "Portugees", ("portuguese", "portugees")),
    ("pol", "pl", None, "Pools", ("polish", "pools")),
    ("tur", "tr", None, "Turks", ("turkish", "turks")),
    ("rus", "ru", None, "Russisch", ("russian", "russisch")),
    ("jpn", "ja", None, "Japans", ("japanese", "japans")),
    ("zho", "zh", "chi", "Chinees", ("chinese", "chinees")),
    ("ara", "ar", None, "Arabisch", ("arabic", "arabisch")),
    # Hindi zonder de twee-lettercode "hi": die staat hieronder als kenmerk
    # (hearing impaired). Wie Hindi bedoelt schrijft ".hin".
    ("hin", None, None, "Hindi", ("hindi",)),
    # De overige B/T-paren uit de blueprint. Ze komen hier zelden voor, maar een
    # "cze"-spoor in een oud bestand moet wel hetzelfde ding zijn als "ces".
    ("ces", None, "cze", "Tsjechisch", ("czech", "tsjechisch")),
    ("ell", None, "gre", "Grieks", ("greek", "grieks")),
    ("isl", None, "ice", "IJslands", ("icelandic", "ijslands")),
    ("fas", None, "per", "Perzisch", ("persian", "perzisch", "farsi")),
    ("ron", None, "rum", "Roemeens", ("romanian", "roemeens")),
    ("slk", None, "slo", "Slowaaks", ("slovak", "slowaaks")),
    ("cym", None, "wel", "Welsh", ("welsh", "wels")),
    ("sqi", None, "alb", "Albanees", ("albanian", "albanees")),
    ("hye", None, "arm", "Armeens", ("armenian", "armeens")),
    ("eus", None, "baq", "Baskisch", ("basque", "baskisch")),
    ("mya", None, "bur", "Birmaans", ("burmese", "birmaans")),
    ("kat", None, "geo", "Georgisch", ("georgian", "georgisch")),
    ("mkd", None, "mac", "Macedonisch", ("macedonian", "macedonisch")),
    ("msa", None, "may", "Maleis", ("malay", "maleis")),
    ("bod", None, "tib", "Tibetaans", ("tibetan", "tibetaans")),
    # "und" is een geldige uitkomst, maar alleen als bewuste keuze van de
    # gebruiker (een tegel in de pagina, --lang und in de CLI). Er wordt nooit
    # vanzelf naar teruggevallen; zie de blueprint, Opbouw 3b.
    ("und", None, None, "Onbekend", ("undetermined", "onbekend", "unknown")),
]

# alias (kleine letters) -> T-code, voor normalize()
_ALIAS: Dict[str, str] = {}
# Idem, maar zonder "und": dit is de tabel waar parse_tag in kijkt. "und"
# betekent "we weten het niet", en dat is nooit iets wat je uit een bestandsnaam
# mag afleiden - een film.und.srt met source "name" zou de bulkweg doen
# inbouwen met het label und, en de pagina de knoppenrij niet doen openklappen.
# Als bewuste keuze mag und wél: --lang und en de tegel in de pagina lopen langs
# normalize(), niet langs parse_tag. Zie de blueprint, Opbouw 3b.
_TAG_ALIAS: Dict[str, str] = {}
# T-code -> weergavenaam
_DISPLAY: Dict[str, str] = {}
for _t, _iso1, _b, _name, _names in _TABLE:
    _DISPLAY[_t] = _name
    for _key in (_t, _iso1, _b, *_names):
        if not _key:
            continue
        if _key in _ALIAS and _ALIAS[_key] != _t:
            raise RuntimeError(f"taaltabel: {_key!r} wijst naar twee talen")
        _ALIAS[_key] = _t
        if _t != "und":
            _TAG_ALIAS[_key] = _t

# Kenmerken in een bestandsnaam. Ze worden vóór de taaltabel opgezocht, zodat
# een kenmerk nooit als taal kan eindigen. Dat is precies waarom ".hi" hier
# hearing impaired betekent en geen Hindi: in deze collectie is een
# SDH-markering waarschijnlijker dan een Hindi-ondertitel.
_MARKERS = {
    "forced": "forced",
    "sdh": "hearing_impaired",
    "hi": "hearing_impaired",
    "cc": "hearing_impaired",
    "default": "default",
    # Herkend, maar zonder vlag. Ze staan hier zodat ze niet als onbekend woord
    # geteld worden: dat verschil bepaalt of de herkomstkolom "niet herkend"
    # toont (er stond iets onbegrijpelijks) of "bevat geen taal" (de naam was
    # prima, er zat alleen geen taal in).
    "full": None,
    "foreign": None,
    "orig": None,
    "original": None,
}

# Scheidingstekens tussen stam en label en tussen de stukken van het label.
_SEPARATORS = ".-_ "
# Met finditer in plaats van split houden we de plaats van elk stuk bij; die is
# nodig om te kunnen melden uit welk stuk van de naam de taal kwam ("uit .Dutch").
_TOKEN = re.compile(r"[^.\-_ ]+")


# --------------------------------------------------------------- functies

def normalize(text: Optional[str]) -> Optional[str]:
    """Elke bekende schrijfwijze naar één ISO 639-2/T-code, of None.

    Witte lijst: wat hier uit komt is per definitie een code uit de tabel
    hierboven, dus drie kleine letters. Alleen zo'n uitkomst mag doorgegeven
    worden aan een --language-argument van mkvmerge; aangeleverde tekst nooit.
    """
    if not isinstance(text, str):
        return None
    key = text.strip().lower()
    if not key:
        return None
    if key in _ALIAS:
        return _ALIAS[key]
    # IETF-vormen als "pt-BR" of "en_US": de gewesttaal past niet in de oude
    # drie-lettercode die Matroska verwacht, dus alleen het taaldeel telt.
    # In parse_tag is het label al gesplitst; dit is voor --lang en voor
    # language_ietf uit mkvmerge.
    head = re.split(r"[-_]", key, maxsplit=1)[0]
    return _ALIAS.get(head)


def display(code: Optional[str]) -> str:
    """Weergavenaam bij een code. Onbekend of leeg -> "Onbekend".

    Dit is de enige bron voor --track-name. De naam komt dus nooit uit een
    bestandsnaam, en rare tekens uit zo'n naam belanden niet in de metadata
    van het mediabestand.
    """
    t = normalize(code)
    return _DISPLAY.get(t, "Onbekend") if t else "Onbekend"


def parse_tag(tag: str) -> dict:
    """Ontleedt het label achter de videostam, bv. ".nl.forced".

    Geeft: language (T-code of None), source, forced, hearing_impaired,
    default, tokens, origin (het stuk waar de taal uit kwam) en origin_text
    (dezelfde uitleg in woorden, voor de logregel en de scan-tabel).

    language is nooit "und": een naam mag geen "we weten het niet" opleveren,
    dat is een keuze van de gebruiker. Zie _TAG_ALIAS.
    """
    out = {
        "tag": tag,
        "language": None,
        "source": "none",
        "forced": False,
        "hearing_impaired": False,
        "default": False,
        "tokens": [],
        "origin": None,
        "origin_text": "geen label",
    }
    if not isinstance(tag, str):
        return out

    parts = [(m.group(), m.start()) for m in _TOKEN.finditer(tag)]
    out["tokens"] = [p for p, _ in parts]
    if not parts:
        return out

    found: List[str] = []          # gevonden T-codes, in volgorde
    origin_of: Dict[str, str] = {}  # T-code -> het oorspronkelijke stuk, met scheidingsteken
    unknown = 0                     # stukken die geen taal, kenmerk of getal waren
    for part, at in parts:
        low = part.lower()
        if low in _MARKERS:
            flag = _MARKERS[low]
            if flag:
                out[flag] = True
            continue
        if low.isdigit():  # ".2" is een teller, geen taal
            continue
        # _TAG_ALIAS en niet normalize(): "und" mag hier geen taal opleveren.
        # De stukken zijn door _TOKEN al op - en _ gesplitst, dus de
        # IETF-afhandeling van normalize() is hier niet nodig.
        code = _TAG_ALIAS.get(low)
        if code:
            if code not in found:
                found.append(code)
                # Het scheidingsteken dat vóór dit stuk stond, zodat de logregel
                # "uit .Dutch" kan tonen en niet alleen "Dutch".
                sep = tag[at - 1] if at > 0 else ""
                origin_of[code] = f"{sep}{part}"
            continue
        unknown += 1

    if len(found) == 1:
        code = found[0]
        out["language"] = code
        out["source"] = "name"
        out["origin"] = origin_of[code]
        # Zonder aanhalingstekens: dit is de vorm die de blueprint zelf twee keer
        # als voorbeeld geeft ("Taal: Nederlands · uit .nl" in §3b, "uit .Dutch"
        # in de scan-tabel van §6). De "niet herkend"/"bevat geen taal"-teksten
        # hieronder citeren wél met aanhalingstekens - dat is een ander geval:
        # daar wordt een onherkend label geciteerd (dus moet het duidelijk als
        # citaat te zien zijn), hier alleen het stukje naam waar de taal wél uit
        # kwam, dat al met een scheidingsteken begint en dus zelfstandig leesbaar is.
        out["origin_text"] = f"uit {origin_of[code]}"
    elif len(found) > 1:
        # Bewust conservatief: "film.nl.en.srt" betekent net zo goed "nl+en
        # samen" als "de nl-versie van de en-release". Gokken kost hier een
        # onherroepelijk herschreven mediabestand.
        out["source"] = "ambiguous"
        out["origin_text"] = "meer dan één taal in de naam"
    elif unknown:
        out["source"] = "unmatched"
        out["origin_text"] = f'label "{tag}" niet herkend'
    else:
        # Alles in het label was herkenbaar (een kenmerk of een teller), er zat
        # alleen geen taal bij: "film.forced.srt". Zelfde source als hierboven,
        # want er valt evenmin een taal uit af te leiden, maar de herkomstkolom
        # van convert.py scan moet het verschil met ".subs" wel kunnen tonen -
        # bij het ene pas je de naam aan, bij het andere weet je al wat het is.
        out["source"] = "unmatched"
        out["origin_text"] = f'label "{tag}" bevat geen taal'
    return out


# De source-waarden die detect_srt kan teruggeven. "no-match" hoort niet bij de
# vier uit de blueprint (name, none, unmatched, ambiguous): die vier gaan over
# een bestand dat wél bij de video hoort. Een afgewezen bestand krijgt bewust
# een waarde die daar niet tussen zit, zodat een lezer die alleen naar source
# kijkt er nooit per ongeluk een taaloordeel uit haalt.
SOURCE_NO_MATCH = "no-match"


def _rejected(out: dict, reason: str, code: str) -> dict:
    """Vult de afwijzingsvelden in. Eén plek, zodat de sleutels overal gelijk zijn."""
    out["reason"] = reason
    out["reason_code"] = code
    out["source"] = SOURCE_NO_MATCH
    out["origin_text"] = reason
    return out


def detect_srt(video_stem: str, srt_name: str) -> dict:
    """Het volledige oordeel over één .srt naast één video.

    De sleutelverzameling is altijd dezelfde, ook bij match=False. Dat is geen
    netheid maar noodzaak: convert.py scan en mkv.ingest lezen deze dict, en een
    sleutel die alleen bij een treffer bestaat geeft een KeyError op precies de
    bestanden die afvallen - de zeldzame tak die je het laatst uitprobeert.

    match=False betekent: dit bestand hoort niet bij deze video. reason zegt
    waarom, reason_code onderscheidt "begint niet met de stam" (dat was nooit
    een treffer) van "grens" (dat wás er vroeger een, en dat is precies wat je
    wilt kunnen naslaan als een srt uit de lijst verdwenen is).
    """
    out = {
        "match": False,
        "reason": "",
        "reason_code": None,
        "tag": "",
        "language": None,
        "source": "none",
        "forced": False,
        "hearing_impaired": False,
        "default": False,
        "tokens": [],
        "origin": None,
        "origin_text": "geen label",
        "display": "Onbekend",
    }
    if not srt_name.lower().endswith(".srt"):
        return _rejected(out, "geen .srt", "suffix")
    if not srt_name.startswith(video_stem):
        return _rejected(out, f"naam begint niet met de videostam {video_stem!r}", "stem")

    tag = srt_name[len(video_stem):-len(".srt")]
    # De grens: wat na de stam komt is leeg (film.srt) of begint met een
    # scheidingsteken. Zonder deze test hoort "Aflevering 10.nl.srt" bij
    # "Aflevering 1.mkv" en "film2.en.srt" bij "film.mkv". Dezelfde grens
    # heeft de taalontleding hieronder toch al nodig.
    if tag and tag[0] not in _SEPARATORS:
        out["tag"] = tag
        return _rejected(out, f'wat na de stam komt ("{tag}") begint niet met '
                              f'een punt, streepje, liggend streepje of spatie',
                         "boundary")

    out.update(parse_tag(tag))
    out["match"] = True
    out["reason"] = ""
    out["reason_code"] = None
    out["display"] = display(out["language"]) if out["language"] else "Onbekend"
    return out


# --------------------------------------------------------------- zelftest

# De tabel uit de blueprint, Opbouw 3b. Video is telkens "film.mkv".
_CASES = [
    # (bestandsnaam, taal, source, extra velden die moeten kloppen)
    # origin_text hier expliciet op tekst getest, net als bij "unmatched"
    # hieronder - anders bewaakt niets de vorm ("uit .nl", geen aanhalingstekens,
    # zie de reparatie van keuringsbevinding KLEIN 2).
    ("film.nl.srt", "nld", "name", {"origin_text": "uit .nl"}),
    ("film.en.srt", "eng", "name", {}),
    ("film.dut.srt", "nld", "name", {}),
    ("film.nld.srt", "nld", "name", {}),
    ("film.eng.srt", "eng", "name", {}),
    ("film.Dutch.srt", "nld", "name", {}),
    ("film.srt", None, "none", {}),
    ("film.subs.srt", None, "unmatched", {"origin_text": 'label ".subs" niet herkend'}),
    # Een label dat alleen een kenmerk bevat is iets anders dan een label dat
    # niemand thuis kan brengen: bij ".forced" hoef je de naam niet te gaan
    # verbeteren, bij ".subs" wel. Zelfde source, andere herkomstregel.
    ("film.forced.srt", None, "unmatched",
     {"forced": True, "origin_text": 'label ".forced" bevat geen taal'}),
    ("film.nl.forced.srt", "nld", "name", {"forced": True}),
    ("film.forced.nl.srt", "nld", "name", {"forced": True}),
    ("film.nl.sdh.srt", "nld", "name", {"hearing_impaired": True}),
    ("film.hi.srt", None, "unmatched", {"hearing_impaired": True}),
    ("film.hin.srt", "hin", "name", {"hearing_impaired": False}),
    ("film.nl.en.srt", None, "ambiguous", {}),
    ("film.nl.nld.srt", "nld", "name", {}),
    ("film.pt-BR.srt", "por", "name", {}),
    ("film.nl.2.srt", "nld", "name", {}),
    ("film2.nl.srt", None, None,
     {"match": False, "source": SOURCE_NO_MATCH}),  # grenscontrole
    ("film-nl.srt", "nld", "name", {}),
    # "und" mag nooit uit een bestandsnaam komen: dan zou de bulkweg dit bestand
    # inbouwen met het label und in plaats van het over te slaan, en zou de
    # pagina de knoppenrij niet openklappen. Alleen --lang und en de tegel in de
    # pagina mogen und opleveren, en die lopen langs normalize().
    ("film.und.srt", None, "unmatched", {}),
    ("film.unknown.srt", None, "unmatched", {}),
    ("film.onbekend.srt", None, "unmatched", {}),
]


def selftest(verbose: bool = True) -> int:
    """Loopt de tabel af. Geeft het aantal afwijkingen terug (0 = goed)."""
    fails = 0
    checks = 0
    for name, want_lang, want_source, extra in _CASES:
        got = detect_srt("film", name)
        problems = []
        want_match = extra.get("match", True)
        checks += 1
        if got["match"] != want_match:
            problems.append(f"match={got['match']} verwacht {want_match}")
        if want_match:
            if got["language"] != want_lang:
                problems.append(f"language={got['language']!r} verwacht {want_lang!r}")
            if got["source"] != want_source:
                problems.append(f"source={got['source']!r} verwacht {want_source!r}")
        # Ook bij match=False nakijken: juist daar moeten source en origin_text
        # de afwijzing verklaren in plaats van "geen label" te blijven zeggen.
        for key, val in extra.items():
            if key == "match":
                continue
            if got[key] != val:
                problems.append(f"{key}={got[key]!r} verwacht {val!r}")
        if problems:
            fails += 1
            print(f"FOUT  {name}: {'; '.join(problems)}", file=sys.stderr)
        elif verbose:
            detail = f"{got['language']} ({got['source']})" if got["match"] else "hoort er niet bij"
            print(f"ok    {name:24s} {detail}")

    # De twee losse gelijkheidstests uit de blueprint, plus und: die moet langs
    # normalize() wél werken - dat is de bewuste weg (--lang und, de tegel).
    for left, right in (("dut", "nld"), ("DUT", "Nederlands"),
                        ("und", "Onbekend"), ("UND", "undetermined")):
        checks += 1
        a, b = normalize(left), normalize(right)
        if a != b or a is None:
            fails += 1
            print(f"FOUT  normalize({left!r})={a!r} != normalize({right!r})={b!r}", file=sys.stderr)
        elif verbose:
            print(f"ok    normalize({left!r}) == normalize({right!r}) == {a!r}")

    # De sleutelverzameling moet gelijk zijn of een afgewezen bestand geeft een
    # KeyError bij de lezer (convert.py scan, mkv.ingest) in plaats van een net
    # oordeel "hoort er niet bij".
    checks += 1
    hit = set(detect_srt("film", "film.nl.srt"))
    miss = set(detect_srt("film", "film2.nl.srt"))
    other = set(detect_srt("film", "film.txt"))
    if hit != miss or hit != other:
        fails += 1
        print(f"FOUT  detect_srt geeft niet altijd dezelfde sleutels: "
              f"treffer-alleen={sorted(hit - miss | hit - other)}, "
              f"afwijzing-alleen={sorted(miss - hit | other - hit)}", file=sys.stderr)
    elif verbose:
        print(f"ok    detect_srt geeft altijd {len(hit)} sleutels, ook bij een afwijzing")

    if verbose:
        print(f"\n{checks} gevallen, {fails} afwijking(en)")
    return fails


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(1 if selftest() else 0)
    print("gebruik: python3 lang.py --selftest", file=sys.stderr)
    sys.exit(2)
