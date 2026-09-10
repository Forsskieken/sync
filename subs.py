#!/usr/bin/env python3
"""
subs.py - lezen en schrijven van SubRip-ondertitels.

Staat los van app.py omdat mkv.py en convert.py dezelfde functies nodig hebben
en app.py niet mogen importeren: dat zou een kringimport geven en zou FastAPI
de CLI in trekken. Deze module kent daarom geen HTTP en geen FastAPI; fouten
gaan als gewone uitzonderingen naar boven.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import List

import lang

log = logging.getLogger("subs")

# ---------------------------------------------------------------- srt

def tc_to_ms(tc: str) -> int:
    tc = tc.strip().replace(".", ",")
    h, m, rest = tc.split(":")
    s, ms = rest.split(",")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def ms_to_tc(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# De blokscheiding van SubRip: een regelovergang, een regel die niets dan
# witruimte bevat, en weer een regelovergang. \s dekt daarbij meer dan spatie en
# tab - ook \f, \v en Unicode-witruimte zoals U+00A0 - en render_srt moet exact
# hetzelfde patroon inklappen, anders schrijft de ene kant iets weg wat de
# andere kant weer als twee blokken leest.
_BLOCK_SPLIT = re.compile(r"\n\s*\n")
# Losse \r en \r\n worden eerst LF; oude Mac-bestanden en Windows-bestanden
# splitsen daarna op precies dezelfde plaatsen.
_CR = re.compile(r"\r\n?")


def parse_srt(text: str, source: str = "") -> List[dict]:
    """Leest srt-tekst. `source` is alleen een naam voor de logregel.

    Blokken zonder tijdstempel worden weggegooid - dat is bestaand gedrag en
    blijft zo, maar het verlies wordt geteld en gemeld. Hier gebeurt het
    namelijk: een lege regel middenin een cue maakt van de staart een blok
    zonder tijdstempel, en zonder deze meting merkt niemand dat de tekst na die
    lege regel weg is (blueprint, Opbouw 3: "de srt die de mux in gaat").
    """
    text = _CR.sub("\n", text)
    cues = []
    blocks = 0
    for block in _BLOCK_SPLIT.split(text.strip()):
        if not block.strip():
            continue
        blocks += 1
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        try:
            left, right = lines[ti].split("-->")[:2]
            start = tc_to_ms(left)
            end = tc_to_ms(right.strip().split()[0])
        except (ValueError, IndexError):
            continue
        cues.append({
            "start": start,
            "end": end,
            "text": "\n".join(lines[ti + 1:]).strip(),
        })
    if blocks > len(cues):
        log.warning(
            "%s: %d blok(ken) in de bron, %d cues gelezen - %d blok(ken) zonder "
            "bruikbaar tijdstempel weggegooid (meestal een lege regel middenin "
            "een cue; opslaan schrijft de ingekorte tekst terug)",
            source or "<tekst zonder naam>", blocks, len(cues), blocks - len(cues))
    return cues


# Een lege regel binnen een cuetekst is het blokscheidingsteken van SubRip:
# parse_srt splitst er op, en de helft na die lege regel houdt geen tijdstempel
# over en verdwijnt bij het teruglezen. Dat sluimert al sinds het begin; het
# wordt scherper zodra deze srt de invoer van een mux is, want dan zit het
# verlies meteen in het mediabestand zelf.
#
# Dit is bewust _BLOCK_SPLIT zelf en geen eigen, engere regex: wat de leeskant
# als blokgrens ziet moet de schrijfkant inklappen, anders blijft er precies
# tussen de twee patronen in een geval over dat wél breekt. \s* is gulzig, dus
# een rij van meerdere lege regels valt in één keer weg.
#
# Een lege regel meteen ná het tijdstempel breekt het blok net zo goed, maar
# _BLOCK_SPLIT ziet die niet (er staat geen regelovergang vóór). Vandaar _LEAD.
# Aan de staart is een lege regel onschadelijk - daar staat toch de scheiding
# naar het volgende blok - maar hij levert een lege regel extra in het bestand
# op, dus die gaat mee weg. Verder wordt de tekst niet aangeraakt: geen strip(),
# want spaties binnen een regel zijn de tekst van de gebruiker en het weghalen
# ervan is geen onderdeel van deze verharding.
_LEAD = re.compile(r"\A\s*\n")
_TRAIL = re.compile(r"\n\s*\Z")


def render_srt(cues: List[dict], source: str = "") -> str:
    """Bouwt de srt-tekst. Klapt lege regels binnen een cue in tot één overgang.

    Apart van write_srt omdat mkv.py de tekst rechtstreeks nodig heeft: die weg
    schrijft een werkbestand in de cache en wil daar geen .orig-backup naast.
    `source` staat alleen in de WARN-regel; zonder die naam is bij een bulkrun
    achteraf niet te zeggen om welk bestand het ging.
    """
    blocks = []
    cues_hit = 0   # cues waarin iets ingeklapt is
    lines_hit = 0  # lege stukken in totaal
    blank_only = 0  # cues waarvan de hele tekst alleen witruimte overhield
    for i, c in enumerate(cues, 1):
        # Dezelfde normalisatie als parse_srt: anders komt "regel\r\n\r\nregel"
        # uit een HTTP-verzoek hier ongemoeid doorheen en splitst het blok pas
        # bij het teruglezen, wanneer de \r wél LF geworden is.
        text = _CR.sub("\n", c["text"])
        text, n = _BLOCK_SPLIT.subn("\n", text)
        text, n_lead = _LEAD.subn("", text)
        text, n_trail = _TRAIL.subn("", text)
        n += n_lead + n_trail
        if n:
            cues_hit += 1
            lines_hit += n
        # Een tekst die uitsluitend uit witruimte bestaat ("   ", geen \n erin)
        # is in SRT niet te onderscheiden van de lege regel die twee blokken
        # scheidt: _BLOCK_SPLIT/_LEAD/_TRAIL raken hem niet aan (die vereisen
        # allemaal een \n), dus hij komt letterlijk als "   " op schijf. Bij het
        # eerstvolgende parse_srt valt die spatiereeks samen met de
        # blokscheiding ervoor en erna, en komt de cue terug met text == "" -
        # stil, want het aantal blokken blijft gelijk aan het aantal cues (geen
        # blok gaat verloren, alleen de tekst erin). Door het hier al naar ""
        # te herleiden staat er nooit een bestand op schijf dat een andere
        # tekst toont dan wat een read ervan teruggeeft, en wordt het geteld in
        # plaats van onopgemerkt te blijven.
        if text and not text.strip():
            blank_only += 1
            text = ""
        blocks.append(f"{i}\n{ms_to_tc(c['start'])} --> {ms_to_tc(c['end'])}\n{text}\n")
    if cues_hit:
        log.warning(
            "%s: lege regels binnen een cuetekst ingeklapt, %d stuks in %d van de "
            "%d cues (zonder dat breekt het blok bij het teruglezen in tweeën)",
            source or "<tekst zonder naam>", lines_hit, cues_hit, len(cues))
    if blank_only:
        log.warning(
            "%s: %d cue(s) met tekst die alleen uit witruimte bestond, geschreven "
            "als lege tekst (SRT kan een witruimte-cue niet onderscheiden van de "
            "scheiding tussen blokken; zonder deze stap kwam de cue bij de "
            "eerstvolgende load stilzwijgend leeg terug)",
            source or "<tekst zonder naam>", blank_only)
    return "\n".join(blocks)


def write_srt(path: Path, cues: List[dict]) -> Path:
    """Schrijft de srt. Maakt eenmalig een .orig backup."""
    backup = path.with_name(path.name + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(render_srt(cues, source=str(path)), encoding="utf-8")
    return backup


# ---------------------------------------------------------------- srt's zoeken

def find_srts(video: Path) -> List[dict]:
    """De losse .srt-bestanden die bij deze video horen, met hun taaloordeel.

    Staat hier en niet in app.py omdat convert.py precies dezelfde lijst nodig
    heeft en app.py niet mag importeren.
    """
    if not video.parent.is_dir():
        return []
    out = []
    for f in sorted(video.parent.iterdir()):
        if f.suffix.lower() != ".srt":
            continue
        d = lang.detect_srt(video.stem, f.name)
        if not d["match"]:
            # Alleen de grensgevallen loggen: een naam die niet met de stam
            # begint stond hier nooit in de lijst, maar een naam die er wel mee
            # begint en op de grens afvalt is precies het geval waarvan je je
            # later afvraagt "waar is mijn ondertitel gebleven".
            if d["reason_code"] == "boundary":
                log.debug("%s hoort niet bij %s: %s", f.name, video.name, d["reason"])
            continue
        out.append({
            "path": str(f),
            "name": f.name,
            "language": d["language"],
            "lang_source": d["source"],
            "lang_origin": d["origin_text"],
            "display": d["display"],
            "forced": d["forced"],
            "hearing_impaired": d["hearing_impaired"],
            "tag": d["tag"],
        })
    return out
