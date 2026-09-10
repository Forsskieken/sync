#!/usr/bin/env python3
"""
mkv.py - het hart van de MKV-weg: sporen identificeren, uithalen, muxen en
atomair vervangen.

Kent geen FastAPI en importeert niets uit app.py; dat zou een kringimport
geven (app.py importeert mkv.py) en zou een CLI-import (convert.py) onnodig
FastAPI mee laten slepen. Fouten gaan als MkvError naar boven; app.py
vertaalt die naar een HTTP-fout. Zie blueprint, Opbouw §3.

identify(), extract() en het takenmodel eromheen (stap 3 en 4), build_mux() met
de controles 6a-6e (stap 5), en de volledige vervangingsvolgorde (stap 6) staan
er allemaal in. mkv.ingest() (taal uit de bestandsnaam, de overslaan-controle
op (taal, forced)) volgt in een latere stap.
"""

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import lang
from subs import parse_srt, read_text, render_srt

log = logging.getLogger("mkv")


class MkvError(Exception):
    """Gaat naar app.py, die er een HTTPException van maakt."""


class MkvBusy(MkvError):
    """Er loopt al een taak. `path` is het bestand dat er de oorzaak van is,
    zodat app.py de naam in de HTTP 409 kan zetten (blueprint §3, Taken:
    "hoogstens één taak tegelijk ... een tweede aanvraag krijgt HTTP 409 met
    de naam van het bestand dat bezig is")."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"er loopt al een taak op {Path(path).name}")


class MkvLanguageUnknown(MkvError):
    """De taal van een losse srt kon niet vastgesteld worden en er is geen
    taal opgelegd (blueprint §3b: "web vraagt, bulk slaat over"; §4: de
    afgesproken foutvorm HTTP 422 met code "language_unknown"). Eigen type,
    niet zomaar een MkvError, zodat app.py de gestructureerde 422 kan bouwen
    ({"code": ..., "tag": ..., "reason": ...}) in plaats van een platte
    foutstring - de voorkant herkent "code" en klapt de taaltegels open in
    plaats van een rode melding te tonen."""

    def __init__(self, tag: str, reason: str):
        self.tag = tag
        self.reason = reason
        super().__init__(
            f"taal van {tag or '(geen label)'} kon niet vastgesteld worden ({reason})")


# Dezelfde lijst als browse() in app.py hanteert (blueprint, Veiligheid:
# "alleen bekende extensies"). Hier ook, en niet alleen in app.py: identify()
# en extract() werken op elk containertype dat mkvmerge leest (ook AVI/MP4,
# blueprint §3), dus de lijst hoort bij de module die dat aanroept.
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm"}

# ---------------------------------------------------------------- omgeving

def _env() -> dict:
    """Omgeving voor elke mkvmerge/mkvextract/ffprobe/ffmpeg-aanroep.

    Op deze machine staat LANG=C, en mkvtoolnix v92 breekt daarop af met
    "locale::facet::_S_create_c_locale name not valid" en afsluitcode 134
    (std::runtime_error, geen normale foutafhandeling - het proces crasht).
    Gemeten, niet aangenomen. Een systemd-unit heeft nog kalere omgeving dan
    een inlogshell, dus dit gaat daar zeker spelen. Als bijvangst: voorspelbaar
    Engelstalige uitvoer, wat de voorwaarde is om de #GUI#-regels betrouwbaar
    te kunnen lezen (blueprint §3, "Alle aanroepen met een vaste locale").
    """
    e = os.environ.copy()
    e["LC_ALL"] = "C.UTF-8"
    return e


# ---------------------------------------------------------------- identify

# S_TEXT/UTF8 is rechtstreeks bewerkbaar. ASS/SSA/WebVTT alleen via een
# omzetting naar SubRip (opmaak en plaatsing gaan dan verloren, blueprint
# "Beslist in ronde 2" §1). PGS en VobSub zijn beeldsporen: geen tekst, dus
# niet bewerkbaar, met een reden die de pagina rechtstreeks kan tonen.
_TEXT_CODECS = {"S_TEXT/UTF8"}
_CONVERT_CODECS = {"S_TEXT/ASS", "S_TEXT/SSA", "S_TEXT/WEBVTT"}
_CONVERT_EXT = {"S_TEXT/ASS": ".ass", "S_TEXT/SSA": ".ssa", "S_TEXT/WEBVTT": ".vtt"}
_IMAGE_REASONS = {
    "S_HDMV/PGS": "beeldspoor (PGS), geen tekst",
    "S_VOBSUB": "beeldspoor (VobSub), geen tekst",
}


def _editable(track: dict) -> tuple:
    """(editable, reason) voor één spoor uit mkvmerge -J.

    true voor S_TEXT/UTF8, "convert" voor ASS/SSA/WebVTT, false met reden
    voor PGS/VobSub - exact de indeling uit blueprint §3. Sporen die geen
    ondertitelspoor zijn (video, audio) en ondertitelsporen met een codec die
    hier niet in voorkomt krijgen ook false, met een eigen reden: anders zou
    de pagina een videospoor per ongeluk als "bewerkbaar: onbekend" tonen in
    plaats van gewoon "geen ondertitelspoor".
    """
    if track.get("type") != "subtitles":
        return False, "geen ondertitelspoor"
    codec_id = (track.get("properties") or {}).get("codec_id", "")
    if codec_id in _TEXT_CODECS:
        return True, None
    if codec_id in _CONVERT_CODECS:
        return "convert", None
    if codec_id in _IMAGE_REASONS:
        return False, _IMAGE_REASONS[codec_id]
    return False, f"onbekende ondertitelcodec ({codec_id or 'geen codec_id'})"


def _mkvmerge_json(path: Path) -> dict:
    """Draait `mkvmerge -J <pad>` en geeft de ruwe JSON terug. Losgetrokken uit
    identify() zodat de verificatie na het muxen (_mux_to_chunk, stap 6b) en
    identify() zelf niet twee keer dezelfde subprocess/foutafhandeling nodig
    hebben - identify() normaliseert en verkleint de ruwe JSON, de verificatie
    heeft juist de ruwe properties nodig (bv. of "language_ietf" er wel is).
    """
    r = subprocess.run(
        ["mkvmerge", "-J", str(path)],
        capture_output=True, text=True, env=_env(),
    )
    # mkvmerge -J geeft ook bij afsluitcode 1 (waarschuwingen, bv. een rare
    # tag) nog bruikbare JSON op stdout. Pas bij 2 (fout) is er niets te lezen.
    if r.returncode not in (0, 1) or not r.stdout.strip():
        raise MkvError(
            f"mkvmerge kon {path.name} niet identificeren (afsluitcode "
            f"{r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise MkvError(f"mkvmerge gaf geen leesbare JSON voor {path.name}: {e}") from e


def identify(path: Path) -> dict:
    """Sporenlijst van path, via `mkvmerge -J`. Werkt ook op AVI/MP4 (mkvmerge
    leest die), zodat mkv.ingest() dezelfde functie gebruikt als de webweg.

    Geeft {"path": ..., "tracks": [...]}, met per spoor id, type, codec,
    codec_id, language, name, default, forced, hearing_impaired, editable,
    reason - blueprint §3. hearing_impaired staat niet met zoveel woorden in
    de blueprinttekst van identify(), maar §3b ("Bestaand spoor vervangen")
    vraagt om precies dat veld van het OUDE spoor over te nemen bij het
    vervangen - zonder deze regel zou replace_subtitle() de SDH-vlag van een
    spoor stilzwijgend laten vallen.
    """
    data = _mkvmerge_json(path)
    tracks = []
    for t in data.get("tracks", []):
        props = t.get("properties", {}) or {}
        editable, reason = _editable(t)
        # language_ietf is wat mkvmerge zelf teruggeeft na --language 0:nld
        # (namelijk "nl"); het oude drieletterveld "language" komt dan terug
        # als de B-code ("dut") - gemeten, zie blueprint §3b. IETF heeft dus
        # voorrang, en beide gaan door normalize() zodat een spoor dat wijzelf
        # ooit geschreven hebben en een vers "nld"-spoor dezelfde taal zijn.
        raw_lang = props.get("language_ietf") or props.get("language")
        code = lang.normalize(raw_lang) or "und"
        tracks.append({
            "id": t.get("id"),
            "type": t.get("type"),
            "codec": t.get("codec"),
            "codec_id": props.get("codec_id"),
            "language": code,
            # Alleen om de voorkant een naam te geven zonder de taaltabel in
            # JavaScript te dupliceren (blueprint, Keuzes: duplicatie van
            # precies dit soort code is hier het gevaarlijkst). display() blijft
            # zo de enige plek waar een code een naam wordt, ook voor de
            # "Taal: ... van het bestaande spoor"-regel in sync.html.
            "language_display": lang.display(code),
            "name": props.get("track_name"),
            "default": bool(props.get("default_track")),
            "forced": bool(props.get("forced_track")),
            # Gemeten property-naam (mkvmerge -J op een spoor gemuxt met
            # --hearing-impaired-flag 0:1): "flag_hearing_impaired".
            "hearing_impaired": bool(props.get("flag_hearing_impaired")),
            "editable": editable,
            "reason": reason,
        })
    return {
        "path": str(path),
        # Bestandsgrootte van de container erbij: de voorkant heeft dit nodig
        # voor de bevestigingstekst "Bevestig · herschrijft 4,2 GB" (blueprint
        # §5) en anders had er een zesde endpoint bij gemoeten om alleen een
        # grootte op te vragen. identify() heeft het bestand toch al ge-stat'
        # via mkvmerge, dus dit kost geen extra aanroep.
        "size": path.stat().st_size,
        "tracks": tracks,
    }


# ---------------------------------------------------------------- uithalen

def _cache_key(path: Path) -> str:
    """Dezelfde sleutelvorm als cache_key() in app.py: sha1(pad:mtime_ns)[:16].

    Losstaande implementatie omdat mkv.py app.py niet mag importeren
    (kringimport - app.py importeert juist mkv.py). Verandert het bestand
    (nieuwe mtime), dan verandert de sleutel vanzelf mee: een tweede keer
    hetzelfde bestand openen is dus gratis, een gewijzigd bestand krijgt vanzelf
    een vers werkbestand (blueprint §3, "Uithalen").
    """
    return hashlib.sha1(f"{path}:{path.stat().st_mtime_ns}".encode()).hexdigest()[:16]


def cache_path(cache_dir: Path, path: Path, track_id: int) -> Path:
    """CACHE_DIR/embedded/<sleutel>-t<id>.srt - blueprint §3, "Uithalen"."""
    return cache_dir / "embedded" / f"{_cache_key(path)}-t{track_id}.srt"


_GUI_PROGRESS = re.compile(r"#GUI#progress\s+(\d+)%")


def _run_gui_mode(cmd: List[str], job: dict, label: str) -> None:
    """Draait een mkvtoolnix-commando met --gui-mode en volgt de voortgang.

    Geldt voor mkvmerge én mkvextract: beide schrijven #GUI#progress N% naar
    stdout, en ook #GUI#warning/#GUI#error - gemeten op v92 door de optie
    echt uit te voeren, want ze staat bij geen van beide in --help. Er is dus
    één voortgangsweg voor uithalen én muxen (blueprint §3, "Taken").

    Update ook de vastloper-klok van het module: 15 minuten geen beweging in
    het percentage laat de vastloper-bewaking het proces doden (zie _watchdog).
    """
    global _PROC, _LAST_PROGRESS, _STALL_REASON
    _STALL_REASON = None
    log.debug("start: %s", " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=_env())
    with _LOCK:
        _PROC = p
        _LAST_PROGRESS = time.monotonic()

    # stderr in een eigen thread meelezen (keuringsbevinding MATIG 3). Alleen
    # p.stdout werd gelezen terwijl het proces liep; stderr pas na p.wait().
    # Loopt de stderr-pijp intussen vol (doorgaans 64 KiB), dan blokkeert het
    # kindproces op zijn eigen write() en komt er ook geen stdout meer, tot de
    # vastloper-bewaking het na 15 minuten doodt. Onbevestigd of mkvmerge/
    # mkvextract met --gui-mode ooit genoeg op stderr schrijven om dat te
    # raken, maar de bescherming is goedkoop en de download-thread stopt
    # vanzelf zodra het kindproces zijn stderr-fd sluit (EOF op p.stderr).
    stderr_lines: List[str] = []

    def _drain_stderr() -> None:
        for line in p.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True,
                                     name="mkv-stderr-drain")
    stderr_thread.start()

    last_percent = job.get("percent", 0)
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            m = _GUI_PROGRESS.match(line)
            if m:
                # 99 % als plafond zolang het proces loopt: 100 % betekent hier
                # "klaar en gecontroleerd", niet "de laatste regel voortgang
                # ontvangen" - anders toont de balk 100 % terwijl de job-runner
                # nog met cues/controles bezig is.
                percent = min(99, int(m.group(1)))
                if percent != last_percent:
                    job["percent"] = percent
                    last_percent = percent
                    with _LOCK:
                        _LAST_PROGRESS = time.monotonic()
            elif line.startswith("#GUI#warning"):
                log.warning("%s: %s", label, line)
                job["log_tail"] = (job["log_tail"] + [line])[-20:]
            elif line.startswith("#GUI#error"):
                # Gemeten (zie mkv.py-commentaar bij MkvError hieronder, en
                # het keuringsverslag): in 16 geconstrueerde gevallen - een
                # missend bestand, een onbestaand spoor-id, een corrupt
                # bronbestand, een ongeldige chapters/attachments-opgave, een
                # onbeschrijfbare doelmap, ontbrekende leesrechten, zowel bij
                # mkvmerge als mkvextract - ging #GUI#error ALTIJD samen met
                # afsluitcode 2, nooit met 0 of 1. Dat is ook wat je verwacht
                # van mkvtoolnix' eigen opzet: de foutfunctie die #GUI#error
                # schrijft (mxerror) breekt het proces intern altijd af.
                # Bevinding KLEIN 7 was daarmee een vermoeden, geen
                # vaststaande fout - de afsluitcode-controle hieronder is dus
                # voldoende en er komt geen aparte #GUI#error-controle bij.
                log.error("%s: %s", label, line)
                job["log_tail"] = (job["log_tail"] + [line])[-20:]
        p.wait()
    finally:
        with _LOCK:
            _PROC = None
        # p.wait() is al terug, dus de stderr-fd van het kindproces is dicht
        # en de leeslus hierboven zit al op EOF of komt er binnen milliseconden
        # aan - de timeout is alleen een noodrem tegen een thread die door een
        # onvoorziene reden niet teruggekomen is.
        stderr_thread.join(timeout=5)

    # De vastloper-bewaking kan _STALL_REASON gezet hebben (en p.kill()
    # geprobeerd hebben) op exact het moment dat het kindproces op eigen
    # kracht klaar was - bijvoorbeeld een grote mux die tijdens het schrijven
    # van cues/seek head geen #GUI#progress meer geeft maar niet vastzit.
    # p.kill() op een proces dat al (misschien nog niet gereaped) afgesloten
    # is, geeft dan GEEN ProcessLookupError - Popen.kill() is een stille no-op
    # zodra het al een returncode heeft. Het enige betrouwbare bewijs dat het
    # kindproces WERKELIJK gedood is, is zijn eigen afsluitcode: bij een
    # signaal (SIGKILL) levert p.wait() een NEGATIEVE returncode (het
    # signaalnummer, POSIX-afspraak); mkvmerge/mkvextract geven zelf nooit een
    # negatieve code. Zonder deze voorwaarde werd hier elke keer dat de
    # bewaking "wilde" ingrijpen een fout gemeld, ook als de taak in
    # werkelijkheid gewoon geslaagd was (keuringsbevinding MATIG 2).
    if _STALL_REASON and p.returncode is not None and p.returncode < 0:
        raise MkvError(_STALL_REASON)
    if _STALL_REASON:
        log.warning(
            "%s: vastloper-bewaking wilde ingrijpen maar %s was op eigen "
            "kracht al klaar (afsluitcode %s) - geen stall, de taak gaat "
            "gewoon door", label, cmd[0], p.returncode)

    stderr_tail = "".join(stderr_lines).strip()[-300:]
    if p.returncode == 1:
        # 1 = waarschuwingen, de taak gaat door (zelfde afspraak als mkvmerge,
        # bevestigd in de man page van mkvextract: 0 ok, 1 waarschuwingen,
        # 2 fout).
        log.warning("%s: %s gaf waarschuwingen (afsluitcode 1)", label, cmd[0])
    elif p.returncode != 0:
        detail = job["log_tail"][-1] if job["log_tail"] else (stderr_tail or "geen foutmelding")
        raise MkvError(f"{cmd[0]} faalde op {label} (afsluitcode {p.returncode}): {detail}")


def _extract_track(path: Path, track_id: int, dest: Path, job: dict) -> None:
    cmd = ["mkvextract", str(path), "--gui-mode", "tracks", f"{track_id}:{dest}"]
    _run_gui_mode(cmd, job, label=path.name)


def _convert_to_srt(raw: Path, dest: Path, label: str) -> None:
    """ASS/SSA/WebVTT -> SubRip met ffmpeg: bestand naar bestand, geen
    spoornummers in het spel (blueprint, Werkverdeling mkvtoolnix/ffmpeg)."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(raw), str(dest)],
        capture_output=True, text=True, env=_env(),
    )
    if r.returncode != 0 or not dest.exists():
        raise MkvError(
            f"ffmpeg kon het ondertitelspoor van {label} niet naar srt omzetten: "
            f"{(r.stderr or '').strip()[-300:]}")


def extract(path: Path, track_id: int, dest: Path, job: Optional[dict] = None) -> Path:
    """Haalt track_id uit path naar dest (altijd .srt); converteert onderweg
    als het spoor geen SubRip is. Synchroon en blokkerend - submit_extract()
    roept dit alleen in een eigen thread aan (blueprint §3, "Uithalen": "op
    een grote MKV over CIFS is dat tientallen seconden ... ook een taak met
    voortgang, geen synchrone aanroep").

    `job` is optioneel en dient alleen om voortgang in te posten; zonder job
    (een wegwerp-dict) werkt de functie identiek, alleen zonder dat iemand het
    percentage ziet oplopen.
    """
    if job is None:
        job = _new_job("extract", path)

    info = identify(path)
    track = next((t for t in info["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise MkvError(f"spoor {track_id} bestaat niet in {path.name}")
    if track["editable"] is False:
        raise MkvError(
            f"spoor {track_id} in {path.name} is niet bewerkbaar: "
            f"{track['reason'] or 'onbekende reden'}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    codec_id = track["codec_id"]
    if codec_id in _TEXT_CODECS:
        _extract_track(path, track_id, dest, job)
    else:
        # ASS/SSA/WebVTT: eerst in eigen vorm uithalen, dan met ffmpeg naar srt.
        # De .tmp-tussenstap staat in dezelfde map als dest (CACHE_DIR/embedded)
        # en wordt hierna opgeruimd; hij is nooit het bestand dat de pagina ziet.
        raw = dest.with_suffix(_CONVERT_EXT[codec_id])
        _extract_track(path, track_id, raw, job)
        job["phase"] = "omzetten"
        try:
            _convert_to_srt(raw, dest, path.name)
        finally:
            raw.unlink(missing_ok=True)
    return dest


# ---------------------------------------------------------------- muxen

_MKVMERGE_VERSION_RE = re.compile(r"v(\d+)\.")
_mkvmerge_major: Optional[int] = None
_mkvmerge_major_lock = threading.Lock()


def mkvmerge_major_version() -> int:
    """Leest `mkvmerge --version`, cached na de eerste keer en op INFO gelogd
    (blueprint §3, "Versieafhankelijkheden": "af te vangen door bij het
    opstarten één keer mkvmerge --version te lezen, het hoofdversienummer te
    bewaren en op INFO te loggen"). build_mux() heeft dit nodig om de juiste
    vlagnamen te kiezen: --default-track-flag/--forced-display-flag bestaan
    pas vanaf 68 (--default-track/--forced-track daarvoor),
    --hearing-impaired-flag bestaat pas vanaf 68 en heeft geen voorganger.
    Onder 50 weigeren we de tool met een duidelijke melding in plaats van
    onvoorspelbaar gedrag te riskeren.
    """
    global _mkvmerge_major
    with _mkvmerge_major_lock:
        if _mkvmerge_major is not None:
            return _mkvmerge_major
        r = subprocess.run(["mkvmerge", "--version"], capture_output=True,
                           text=True, env=_env())
        if r.returncode != 0:
            raise MkvError(
                f"mkvmerge --version faalde (afsluitcode {r.returncode}): "
                f"{(r.stderr or r.stdout).strip()[-300:]}")
        m = _MKVMERGE_VERSION_RE.search(r.stdout)
        if not m:
            raise MkvError(f"kon geen versienummer uit 'mkvmerge --version' "
                           f"halen: {r.stdout.strip()!r}")
        major = int(m.group(1))
        if major < 50:
            raise MkvError(
                f"mkvtoolnix versie {major} is te oud voor de MKV-weg "
                f"(minstens 50 vereist, {r.stdout.strip()})")
        log.info("mkvmerge versie gedetecteerd: %s (hoofdversie %d)",
                 r.stdout.strip(), major)
        _mkvmerge_major = major
        return major


def build_mux(source: Path, drop_track_ids: List[int], add_srt: Optional[Path],
              out: Path, label: dict, major: Optional[int] = None) -> List[str]:
    """Bouwt de mkvmerge-argumentenlijst voor alle drie de gevallen uit
    blueprint §3 ("Muxen"): een bestaand tekstspoor vervangen (drop_track_ids
    + add_srt), een losse srt in een bestaande MKV steken (alleen add_srt), of
    een AVI/MP4 omzetten (source is dan het AVI/MP4-bestand, drop_track_ids
    leeg). label bepaalt taal, spoornaam en de drie vlaggen; build_mux beslist
    zelf niets over taal ("label is de enige plek waar taal en vlaggen vandaan
    komen").

    De volgorde is dwingend: mkvmerge leest argumenten links-naar-rechts en
    past een optie toe op het eerstvolgende bestand op de commandoregel, dus
    opties die op een invoerbestand slaan staan vóór dat bestand.
    """
    if major is None:
        major = mkvmerge_major_version()

    cmd = ["mkvmerge", "--gui-mode", "-o", str(out)]
    if drop_track_ids:
        # "!3,5" sluit sporen uit in plaats van in te sluiten (gemeten: werkt,
        # ook al staat de "!"-vorm niet in --help). Zo hoeven we bij een
        # bestand met meerdere ondertitelsporen niet op te sommen wat er WEL
        # mee moet - alleen wat eruit gaat.
        ids = ",".join(str(i) for i in drop_track_ids)
        cmd += ["--subtitle-tracks", f"!{ids}"]
    cmd += [str(source)]

    if add_srt is not None:
        lang_code = label.get("language") or "und"
        # Witte lijst (blueprint, Veiligheid): alleen een code die
        # lang.normalize() zelf teruggeeft mag in een --language-argument
        # terechtkomen. Dit is de laatste plek vóór het subprocess-argument,
        # dus hier controleren, niet erop vertrouwen dat de aanroeper het al
        # deed.
        if lang.normalize(lang_code) != lang_code:
            raise MkvError(f"taalcode {lang_code!r} is geen genormaliseerde "
                           f"code - build_mux weigert dit als --language-argument")
        opts = ["--language", f"0:{lang_code}"]
        name = label.get("track_name")
        if name:
            # display()/de aanroeper leveren de naam, nooit een bestandsnaam
            # rechtstreeks (blueprint, Veiligheid: "de spoornaam wordt
            # evenmin uit de bestandsnaam overgenomen").
            opts += ["--track-name", f"0:{name}"]
        default_flag = "1" if label.get("default") else "0"
        forced_flag = "1" if label.get("forced") else "0"
        if major >= 68:
            opts += ["--default-track-flag", f"0:{default_flag}"]
            opts += ["--forced-display-flag", f"0:{forced_flag}"]
        else:
            opts += ["--default-track", f"0:{default_flag}"]
            opts += ["--forced-track", f"0:{forced_flag}"]
        if label.get("hearing_impaired"):
            if major >= 68:
                opts += ["--hearing-impaired-flag", "0:1"]
            else:
                log.warning(
                    "mkvtoolnix %d kent --hearing-impaired-flag niet (pas "
                    "vanaf 68); alleen het achtervoegsel in de spoornaam "
                    "markeert dit spoor nog als SDH", major)
        cmd += opts + [str(add_srt)]
    return cmd


def _ffprobe_duration(path: Path) -> float:
    """Duur in seconden via ffprobe op containerniveau (blueprint,
    Werkverdeling: "zit er al in, werkt op containerniveau zonder
    spoornummers, dus geen kans op verwarring met de mkvmerge-nummering").

    Los van app.py's probe(): die kijkt naar het eerste AUDIOspoor en geeft
    een HTTPException, dit kijkt naar de container en geeft een MkvError -
    twee contracten voor twee lagen die elkaar niet mogen importeren.
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, env=_env())
    if r.returncode != 0:
        raise MkvError(f"ffprobe kon de duur van {path.name} niet meten: "
                       f"{(r.stderr or '').strip()[-300:]}")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise MkvError(f"ffprobe gaf geen leesbare JSON voor {path.name}: {e}") from e
    duration = (data.get("format") or {}).get("duration")
    # ffprobe LAAT het "duration"-veld weg als de container geen bruikbare
    # duur heeft (een .ts zonder betrouwbare PCR, een beschadigde header) -
    # het schrijft geen "0". `or 0` hierboven verzweeg dat verschil vroeger:
    # een bron zonder duur werd orig_duration_s = 0.0, en als de mux toevallig
    # ook geen duur opleverde (0.0 - 0.0 = 0.0) SLAAGDE controle c op een
    # bestand waarvan de duur nooit echt gemeten is (keuringsbevinding
    # MATIG 1). Vandaar hier een harde weigering in plaats van een stille 0.
    if duration is None:
        raise MkvError(
            f"ffprobe kon geen duur vaststellen voor {path.name} (geen "
            f"'duration'-veld in de containerinfo) - zonder een betrouwbare "
            f"duur kan de duurcontrole na het muxen niet werken, dus dit "
            f"bestand wordt geweigerd. origineel ongewijzigd.")
    try:
        return float(duration)
    except (ValueError, TypeError) as e:
        raise MkvError(f"ffprobe gaf een onleesbare duur voor {path.name}: "
                       f"{duration!r} ({e}). origineel ongewijzigd.") from e


def _cues_text_hash(cues: List[dict]) -> str:
    """SHA-1 over de tekst van alle cues samen, voor controle d.

    Aantal cues en starttijden alleen bewijzen niet dat de TEKST heel bleef -
    een round-trip die de inhoud van elke cue corrumpeert maar het aantal
    blokken en de tijden intact laat, glipt er zonder deze hash ongemerkt
    doorheen (keuringsbevinding MATIG 4, blueprint §3 controle d, "de tekst
    van elke cue gelijk aan wat erin ging"). Een \\x00-scheidingsteken tussen
    cues voorkomt dat een verschuiving van de grens tussen twee cues (cue 1 =
    "AB" + cue 2 = "" versus cue 1 = "A" + cue 2 = "B") toevallig dezelfde
    aaneengesloten byte-reeks oplevert.
    """
    h = hashlib.sha1()
    for c in cues:
        h.update(c["text"].encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _mux_to_chunk(source: Path, chunk: Path, drop_track_ids: List[int],
                  add_srt: Optional[Path], label: dict, orig_info: dict,
                  orig_duration_s: float, cache_dir: Path, job: dict) -> None:
    """Stap 5 (muxen) + stap 6a-6e (controleren) uit de blueprint.

    Schrijft naar `chunk` en laat het bestand staan, ook bij succes - de
    aanroeper (_execute_transaction) beslist over vervangen en ruimt de chunk
    in zijn eigen `finally` op (stap 10). Dat maakt deze functie ook los
    draaibaar tijdens het ontwikkelen: "schrijf naar een brok en laat het
    staan" (blueprint, Stappen voor de implementer, stap 5).

    Bij elke misser: MkvError, en het origineel is op dit punt in de
    vervangingsvolgorde nog nooit aangeraakt - vandaar "origineel
    ongewijzigd" in elke foutmelding hieronder.
    """
    expected_cues = None
    if add_srt is not None:
        expected_cues = parse_srt(read_text(add_srt), source=str(add_srt))

    major = mkvmerge_major_version()
    cmd = build_mux(source, drop_track_ids, add_srt, chunk, label, major)

    job["phase"] = "muxen"
    log.info("muxen gestart: %s -> %s (weg: %s, toegevoegd: %s)",
             source.name, chunk.name, drop_track_ids or "geen", bool(add_srt))
    # Controle a zit al in _run_gui_mode: afsluitcode 1 = waarschuwing (WARN,
    # gaat door), 2 = fout. mkvextract kent dezelfde afspraak (zie de man
    # page-verwijzing daar); we hergebruiken hem hier voor mkvmerge.
    try:
        _run_gui_mode(cmd, job, label=source.name)
    except MkvError as e:
        raise MkvError(f"muxen van {source.name} mislukte: {e}. "
                       f"origineel ongewijzigd.") from e

    job["state"] = "verifying"
    job["phase"] = "controleren"

    # b. Sporentelling per type, plus - amendement op de blueprint, gemeten -
    #    language_ietf moet op elk spoor aanwezig zijn: ffmpeg laat dat veld
    #    juist vallen, dus als het na een mkvmerge-mux zou ontbreken is er
    #    iets grondig mis met de installatie of de versie, en dat wil je NU
    #    weten - niet pas als Jellyfin het verkeerde spoor kiest.
    chunk_raw = _mkvmerge_json(chunk)
    chunk_tracks = chunk_raw.get("tracks", [])

    def _count(tracks, ttype):
        return sum(1 for t in tracks if t.get("type") == ttype)

    # orig_info komt uit identify(): dezelfde "type"-waarden als de ruwe JSON,
    # dus rechtstreeks vergelijkbaar zonder een tweede identify()-aanroep op
    # het origineel.
    orig_counts = {t: _count(orig_info["tracks"], t) for t in ("video", "audio", "subtitles")}
    new_counts = {t: _count(chunk_tracks, t) for t in ("video", "audio", "subtitles")}
    expected_subs = orig_counts["subtitles"] - len(drop_track_ids) + (1 if add_srt else 0)

    problems = []
    if new_counts["video"] != orig_counts["video"]:
        problems.append(f"video {new_counts['video']} spoor/sporen, verwacht {orig_counts['video']}")
    if new_counts["audio"] != orig_counts["audio"]:
        problems.append(f"audio {new_counts['audio']} spoor/sporen, verwacht {orig_counts['audio']}")
    if new_counts["subtitles"] != expected_subs:
        problems.append(
            f"ondertitels {new_counts['subtitles']} spoor/sporen, verwacht {expected_subs} "
            f"({orig_counts['subtitles']} - {len(drop_track_ids)} weg + {1 if add_srt else 0} toegevoegd)")
    missing_ietf = [t.get("id") for t in chunk_tracks
                    if "language_ietf" not in (t.get("properties") or {})]
    if missing_ietf:
        problems.append(f"language_ietf ontbreekt op spoor {missing_ietf} na de mux")

    new_sub_track = None
    if add_srt is not None:
        # Onze eigen srt is altijd het laatste invoerbestand dat build_mux()
        # meegeeft, dus het spoor eruit is in de uitvoer het hoogst genummerde
        # (gemeten: een test-mux van rijk.mkv + een nieuwe srt zette het
        # nieuwe spoor op de laatste id, ook na het weglaten van een bestaand
        # spoor).
        subs = [t for t in chunk_tracks if t.get("type") == "subtitles"]
        new_sub_track = max(subs, key=lambda t: t["id"]) if subs else None
        if new_sub_track is None:
            problems.append("geen ondertitelspoor teruggevonden in het brok na het toevoegen")
        else:
            props = new_sub_track.get("properties") or {}
            # B/T-verschil (blueprint §3b, "gemeten"): --language 0:nld komt
            # terug als language "dut" + language_ietf "nl". Genormaliseerd
            # vergelijken, anders faalt deze controle op ons eigen bestand.
            got_lang = lang.normalize(props.get("language_ietf") or props.get("language"))
            want_lang = label.get("language") or "und"
            if got_lang != want_lang:
                problems.append(f"nieuw spoor heeft taal {got_lang!r}, verwacht {want_lang!r}")

    if problems:
        raise MkvError(
            f"controle na het muxen van {source.name} faalde: {'; '.join(problems)}. "
            f"origineel ongewijzigd.")
    log.info("controle b geslaagd op %s: video %d, audio %d, ondertitels %d "
             "(verwacht %d), language_ietf aanwezig op alle sporen",
             chunk.name, new_counts["video"], new_counts["audio"],
             new_counts["subtitles"], expected_subs)

    # c. Duur - vangt een afgekapt bestand.
    new_duration_s = _ffprobe_duration(chunk)
    diff = abs(new_duration_s - orig_duration_s)
    if diff > 1.0:
        raise MkvError(
            f"duur van {chunk.name} ({new_duration_s:.1f} s) wijkt {diff:.1f} s af van "
            f"{source.name} ({orig_duration_s:.1f} s) - mogelijk een afgekapt bestand. "
            f"origineel ongewijzigd.")
    log.info("controle c geslaagd: duur %.1f s tegen %.1f s (verschil %.3f s)",
             new_duration_s, orig_duration_s, diff)

    # d. Het nieuwe ondertitelspoor er weer uithalen en teruglezen.
    if add_srt is not None and new_sub_track is not None:
        verify_dest = cache_dir / "embedded" / f"{job['id']}-verify.srt"
        verify_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            extract(chunk, new_sub_track["id"], verify_dest)
            got_cues = parse_srt(read_text(verify_dest), source=str(verify_dest))
        finally:
            verify_dest.unlink(missing_ok=True)

        if len(got_cues) != len(expected_cues):
            raise MkvError(
                f"controle op {chunk.name} faalde: {len(got_cues)} cues teruggelezen, "
                f"{len(expected_cues)} erin gestopt. origineel ongewijzigd.")
        worst = max((abs(want["start"] - got["start"])
                    for want, got in zip(expected_cues, got_cues)), default=0)
        if worst > 2:
            raise MkvError(
                f"controle op {chunk.name} faalde: starttijd wijkt tot {worst} ms af na "
                f"de mux (marge is 2 ms). origineel ongewijzigd.")
        # De tekst zelf, niet alleen aantal en tijden (blueprint §3 controle
        # d, ronde 3). Vergeleken wordt op de tekst zoals render_srt hem
        # geschreven heeft: expected_cues komt uit add_srt, en dat bestand is
        # altijd via render_srt geschreven (zie replace_subtitle/ingest) - dus
        # de lege regels die render_srt zelf al inklapt zitten in beide kanten
        # van deze vergelijking. Vergelijken met de ruwe invoer van de
        # gebruiker zou hier onterecht aanslaan op precies dat inklappen.
        want_hash = _cues_text_hash(expected_cues)
        got_hash = _cues_text_hash(got_cues)
        if want_hash != got_hash:
            raise MkvError(
                f"controle op {chunk.name} faalde: de ondertiteltekst is veranderd "
                f"tijdens het muxen (aantal cues en starttijden kloppen wel; "
                f"tekst-hash {got_hash[:12]} tegen verwacht {want_hash[:12]}). "
                f"origineel ongewijzigd.")
        log.info("controle d geslaagd: %d cues, grootste afwijking %d ms, tekst-hash %s",
                 len(got_cues), worst, got_hash[:12])

    # e. Bestandsgrootte - alleen WARN, geen weigering: een AVI -> MKV-omzetting
    #    mag krimpen (blueprint 6e).
    orig_size = source.stat().st_size
    new_size = chunk.stat().st_size
    if orig_size and new_size < 0.70 * orig_size:
        log.warning(
            "%s is na het muxen %.0f%% van de oorspronkelijke grootte (%.1f MB tegen "
            "%.1f MB) - controleer of dat verwacht is",
            chunk.name, 100 * new_size / orig_size, new_size / 1_048_576, orig_size / 1_048_576)


# ---------------------------------------------------------------- taken

# Eén takenmodel voor uithalen én muxen (blueprint §3, "Taken").
JOB_STATES_ACTIVE = {"queued", "running", "verifying"}

_LOCK = threading.Lock()
_CURRENT: Optional[dict] = None          # de ene lopende taak, of None
_DONE: "deque[dict]" = deque(maxlen=20)  # afgeronde taken, voor de pagina

# _LOCK hierboven is een threading.Lock: die geldt alleen BINNEN dit ene
# Python-proces. De blueprint belooft "hoogstens één taak tegelijk, van welke
# soort ook" ook tussen deze dienst en een losse convert.py-run (die nog
# gebouwd moet worden) - twee processen die tegelijk over dezelfde share
# schrijven maken elkaar traag en vergroten de kans op een half-mislukte
# vervanging, precies waar deze belofte voor bedoeld is (keuringsbevinding
# MATIG 5). flock() op één vast lockbestand maakt de belofte ook
# systeemwide: het OS geeft de exclusieve lock aan precies één open
# bestandsbeschrijving, over processen heen, en geeft hem automatisch vrij
# zodra dat proces stopt of crasht - geen aparte "stale lock" opruiming nodig.
#
# Het lockbestand staat in CACHE_DIR en NIET "naast het brok" (zoals de
# blueprinttekst als voorbeeld noemt): de belofte is systeemwide - één taak
# tegelijk, punt, niet "één taak per bestand" - dus één vast pad ongeacht
# welk mediabestand bewerkt wordt. CACHE_DIR is bovendien altijd lokale
# opslag (blueprint, Veiligheid: "CACHE_DIR valt onder CacheDirectory="),
# terwijl het mediabestand op een netwerk-share kan staan waar flock-gedrag
# minder hard gegarandeerd is.
_LOCKFILE_NAME = "mkv.lock"


class _FileLock:
    """flock() op CACHE_DIR/mkv.lock, aanvullend op _LOCK hierboven.

    acquire() is non-blocking (LOCK_NB): een tweede proces krijgt meteen een
    OSError terug in plaats van te wachten, want de blueprint wil een directe
    HTTP 409 ("een tweede aanvraag krijgt HTTP 409 met de naam van het
    bestand dat bezig is"), geen hangende aanvraag. De inhoud van het
    lockbestand is het pad van het bestand waar de houder mee bezig is, zodat
    een concurrerend proces daar de naam voor zijn eigen MkvBusy uit kan
    lezen - anders zou "wie is er precies bezig" onbekend zijn voor een ANDER
    proces (dat heeft geen zicht op _CURRENT, dat is een Python-global van dit
    proces).
    """

    def __init__(self, cache_dir: Path):
        self._path = cache_dir / _LOCKFILE_NAME
        self._fh = None

    def acquire(self, holder_path: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" in plaats van "w": "w" zou het bestand meteen leegmaken, ook
        # als een ander proces de lock al vasthoudt en de open() zelf dus
        # geen enkel recht geeft - dan staat er straks niets meer in voor die
        # houder om te lezen. flock() bepaalt hier het exclusieve recht, niet
        # de open-modus.
        fh = open(self._path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            busy_with = fh.read().strip() or "een ander bestand"
            fh.close()
            raise MkvBusy(busy_with)
        fh.seek(0)
        fh.truncate()
        fh.write(holder_path)
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

# Het kindproces en het moment van de laatste voortgangsmelding, voor de
# vastloper-bewaking. Losstaand van _CURRENT: de job-dict bevat alleen
# JSON-vriendelijke velden die zo de API in gaan, een Popen-object hoort daar
# niet tussen.
_PROC: Optional[subprocess.Popen] = None
_LAST_PROGRESS: float = 0.0
_STALL_REASON: Optional[str] = None

# 15 minuten - blueprint §3, "Vastloper-bewaking". Eerlijk voorbehoud uit
# dezelfde paragraaf: bij een echt bevroren mount hangt het proces in
# ononderbreekbare toestand en helpt geen signaal; dan blijft de taak op zijn
# percentage staan en is dát, plus de WARN/ERROR-regels hier, het signaal.
STALL_TIMEOUT_S = 15 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_job(kind: str, path: Path) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "path": str(path),
        "state": "queued",
        "percent": 0,
        "phase": None,
        "error": None,
        "started": _now(),
        "finished": None,
        "log_tail": [],
    }


def _watchdog() -> None:
    """Achtergrondlus: stopt een kindproces dat 15 minuten geen voortgang
    boekt. Eén doorlopende daemon-thread in plaats van een timer per taak: er
    is toch maar één taak tegelijk (de grendel hieronder), dus volstaat één
    lus die elke 30 s kijkt."""
    global _STALL_REASON
    while True:
        time.sleep(30)
        with _LOCK:
            proc = _PROC
            job = _CURRENT
            idle = time.monotonic() - _LAST_PROGRESS if proc is not None else 0.0
        if proc is None or idle <= STALL_TIMEOUT_S:
            continue
        if proc.poll() is not None:
            # Al vanzelf klaar tussen het meten van "idle" hierboven en nu -
            # geen kindproces meer om te doden. Dit is dezelfde race als
            # hieronder bij proc.kill(), maar dan zo vroeg mogelijk afgevangen
            # in plaats van pas na een nutteloos signaal (keuringsbevinding
            # MATIG 2). De echte scheidsrechter blijft _run_gui_mode's eigen
            # returncode-controle: díe ziet zelfs een kill() die precies in
            # het gaatje van deze check terechtkwam.
            continue
        minutes = int(idle // 60)
        percent = job["percent"] if job else "?"
        _STALL_REASON = f"geen voortgang meer sinds {minutes} minuten (bleef op {percent}%)"
        log.error("mkv: kindproces pid %d al %d minuten zonder voortgang op %s, wordt gestopt",
                  proc.pid, minutes, job["path"] if job else "?")
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # was tussen de poll()-controle hierboven en nu alsnog gestopt


threading.Thread(target=_watchdog, daemon=True, name="mkv-watchdog").start()


def submit_extract(path: Path, track_id: int, cache_dir: Path) -> dict:
    """Start het uithalen van track_id uit path als achtergrondtaak en geeft
    de Job terug. Hoogstens één taak tegelijk (blueprint §3, "Taken"): een
    tweede aanvraag terwijl er al iets loopt geeft MkvBusy, die app.py naar
    HTTP 409 vertaalt.
    """
    info = identify(path)
    track = next((t for t in info["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise MkvError(f"spoor {track_id} bestaat niet in {path.name}")
    if track["editable"] is False:
        raise MkvError(
            f"spoor {track_id} in {path.name} is niet bewerkbaar: "
            f"{track['reason'] or 'onbekende reden'}")

    dest = cache_path(cache_dir, path, track_id)
    if dest.exists():
        # Zelfde sleutel (pad + mtime_ns) als een eerdere extract van dit
        # bestand en spoor: geen mkvextract-aanroep nodig, dus geen slot in de
        # grendel (blueprint §3, "Uithalen": "een tweede keer openen van
        # hetzelfde bestand is gratis").
        job = _new_job("extract", path)
        job["state"] = "done"
        job["percent"] = 100
        job["cues"] = parse_srt(read_text(dest), source=str(dest))
        job["finished"] = _now()
        with _LOCK:
            _DONE.append(job)
        log.info("uithalen (cache): %s spoor %s -> %d cues", path.name, track_id, len(job["cues"]))
        return dict(job)

    global _CURRENT
    # De file-lock wordt VERKREGEN binnen dezelfde with _LOCK-sectie als de
    # in-memory controle: zo blijft "is er al iets bezig" één atomaire
    # beslissing. acquire() is zelf non-blocking (LOCK_NB), dus dit houdt
    # _LOCK geen merkbare tijd vast (keuringsbevinding MATIG 5).
    file_lock = _FileLock(cache_dir)
    with _LOCK:
        if _CURRENT is not None and _CURRENT["state"] in JOB_STATES_ACTIVE:
            raise MkvBusy(_CURRENT["path"])
        file_lock.acquire(str(path))
        job = _new_job("extract", path)
        _CURRENT = job

    threading.Thread(target=_run_extract_job, args=(job, path, track_id, dest, file_lock),
                     daemon=True, name=f"mkv-extract-{job['id'][:8]}").start()
    return dict(job)


def _run_extract_job(job: dict, path: Path, track_id: int, dest: Path,
                     file_lock: "_FileLock") -> None:
    global _CURRENT
    job["state"] = "running"
    job["phase"] = "uithalen"
    try:
        size_mb = path.stat().st_size / 1_048_576
    except OSError:
        size_mb = 0.0
    log.info("uithalen gestart: %s spoor %s, %.1f MB", path.name, track_id, size_mb)
    try:
        extract(path, track_id, dest, job=job)
        job["cues"] = parse_srt(read_text(dest), source=str(dest))
        job["percent"] = 100
        job["phase"] = None
        job["state"] = "done"
        job["finished"] = _now()
        log.info("uithalen klaar: %s spoor %s, %d cues", path.name, track_id, len(job["cues"]))
    except MkvError as e:
        job["state"] = "error"
        job["error"] = str(e)
        job["phase"] = None
        job["finished"] = _now()
        log.error("uithalen mislukt: %s spoor %s: %s", path.name, track_id, e)
    except Exception as e:
        # Een onverwachte fout (bug, niet een MkvError) mag de taak niet
        # stilzwijgend op "running" laten hangen - blueprint, Werking bij
        # fouten: "een fout wordt afgehandeld of doorgegeven, nooit
        # stilzwijgend genegeerd". exc_info zodat de traceback in het
        # journaal staat, niet alleen "onverwachte fout".
        job["state"] = "error"
        job["error"] = f"onverwachte fout: {e}"
        job["phase"] = None
        job["finished"] = _now()
        log.exception("uithalen: onverwachte fout op %s spoor %s", path.name, track_id)
    finally:
        file_lock.release()
        with _LOCK:
            if _CURRENT is job:
                _CURRENT = None
            _DONE.append(job)


def get_job(job_id: str) -> Optional[dict]:
    """Toestand van een taak, lopend of afgerond. None als het id onbekend is
    (nooit bestaan, of allang uit de deque van 20 gerold)."""
    with _LOCK:
        if _CURRENT is not None and _CURRENT["id"] == job_id:
            return dict(_CURRENT)
        for j in _DONE:
            if j["id"] == job_id:
                return dict(j)
    return None


# ---------------------------------------------------------------- vervangen

# 6 uur - blueprint §3, "De vervangingsvolgorde" stap 4. Ruim boven de duur
# van een enkele mux, zodat een brok van de taak die NU loopt nooit als
# verweesd geldt.
_ORPHAN_MAX_AGE_S = 6 * 3600

# .syncpart is het brok tijdens het muxen (stap 5). .syncold is het origineel,
# kortstondig opzij gezet door de terugvalweg van _atomic_replace (stap 8) -
# hoort normaal binnen milliseconden weer te verdwijnen (old.unlink() vlak
# erna), maar als precies DIE unlink faalt bleef zo'n bestand voorheen voor
# altijd liggen, met alleen een losse WARN als spoor (keuringsbevinding
# KLEIN 6). Beide patronen horen daarom bij dezelfde leeftijdsgebaseerde
# opruiming.
_ORPHAN_SUFFIXES = (".syncpart", ".syncold")


def _cleanup_orphans(directory: Path) -> None:
    """Stap 4: brokken en achtergebleven .syncold-bestanden van een gecrashte
    eerdere run opruimen. Alleen in deze ene map (geen wandeling over de hele
    share) en alleen ouder dan 6 uur."""
    now = time.time()
    try:
        entries = list(directory.iterdir())
    except OSError as e:
        log.warning("kon %s niet doorzoeken op verweesde brokken: %s", directory, e)
        return
    for f in entries:
        if not (f.name.startswith(".") and f.name.endswith(_ORPHAN_SUFFIXES)):
            continue
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue  # verdwenen tussen iterdir() en stat() - niets te ruimen
        if age < _ORPHAN_MAX_AGE_S:
            continue
        try:
            f.unlink()
            log.warning("verweesd brok opgeruimd: %s (%.1f uur oud)", f, age / 3600)
        except OSError as e:
            log.warning("kon verweesd brok %s niet opruimen: %s", f, e)


class MkvCatastrophic(MkvError):
    """Stap 8, het dubbele faalpad: zowel het plaatsen van het brok als het
    terugzetten van het origineel na een mislukte os.replace mislukten.
    Extreem zeldzaam - het zou betekenen dat dezelfde map tussen twee
    rename()-aanroepen vlak na elkaar onschrijfbaar werd - en niet met code te
    herstellen. Eigen type zodat de aanroeper de chunk NIET opruimt: dat kan
    op dat moment de enige plek zijn waar de nieuwe inhoud nog staat."""


def _atomic_replace(chunk: Path, target: Path) -> None:
    """Stap 8. Op ZFS is os.replace een gewone rename binnen één dataset en
    dus echt atomair (blueprint, Nog open §1: de shares komen als
    Proxmox-bind-mount van een ZFS-dataset binnen) - dit is de hoofdweg, geen
    "meestal lukt het" en dus ook geen tekst over CIFS meer in de meldingen
    hieronder. De EBUSY/EACCES-terugval is een goedkope verzekering voor als
    het doelbestand toch nog open staat (bv. een speler die nog streamt).
    """
    try:
        os.replace(chunk, target)
        return
    except OSError as e:
        if e.errno not in (errno.EBUSY, errno.EACCES):
            # Elke andere fout hier betekent dat de rename niet is doorgegaan
            # (POSIX-rename is alles-of-niets) - het origineel staat dus nog
            # gewoon op zijn plaats.
            raise MkvError(f"vervangen van {target.name} mislukte: {e}. "
                           f"origineel ongewijzigd.") from e
        log.warning("os.replace op %s gaf %s (%s), terugvalweg via hernoemen",
                    target, errno.errorcode.get(e.errno, e.errno), e)

    old = target.parent / f".{target.name}.syncold"
    try:
        os.replace(target, old)
    except OSError as e:
        raise MkvError(
            f"vervangen mislukte, mogelijk staat de speler nog op dit bestand - "
            f"stop de weergave en probeer opnieuw ({e}). origineel ongewijzigd.") from e

    try:
        os.replace(chunk, target)
    except OSError as e:
        try:
            os.replace(old, target)  # origineel terugzetten
        except OSError as e2:
            raise MkvCatastrophic(
                f"vervangen van {target.name} mislukte ({e}) EN het teruginzetten van "
                f"het origineel ook ({e2}) - het origineel staat nu als {old.name}, het "
                f"brok als {chunk.name}: controleer dit met de hand.") from e2
        raise MkvError(
            f"vervangen mislukte, mogelijk staat de speler nog op dit bestand - "
            f"stop de weergave en probeer opnieuw ({e}). origineel ongewijzigd.") from e

    try:
        old.unlink()
    except OSError as e:
        log.warning("oud bestand %s kon niet verwijderd worden na het vervangen: %s", old, e)


def _execute_transaction(source: Path, target: Path, drop_track_ids: List[int],
                         add_srt: Optional[Path], label: dict, cache_dir: Path,
                         job: dict) -> None:
    """De volledige vervangingsvolgorde, stappen 1-10 (blueprint, Opbouw §3).

    `source` is het bestand dat gelezen wordt, `target` waar het resultaat
    moet staan - gelijk aan `source` bij het vervangen van een spoor (kind
    "replace-sub"), een andere naam/extensie bij een omzetting (kind
    "ingest", stap 8, nog niet gebouwd). Chunk, controles en vervanging
    gebeuren allemaal in source.parent: "dezelfde map, niet alleen hetzelfde
    filesystem - dat is de enige garantie dat de share en dus de rename
    dezelfde is" (stap 5).
    """
    if target.parent != source.parent:
        # Zou nooit mogen gebeuren als de aanroeper zich aan het contract
        # houdt (dezelfde map is de garantie achter een atomaire rename); een
        # eigen controle hier is goedkoper dan een half geschreven bestand op
        # de verkeerde share.
        raise MkvError(f"doelmap ({target.parent}) wijkt af van de bronmap "
                       f"({source.parent}) - de vervangingsvolgorde staat dat niet toe")
    is_rename = target != source
    if is_rename and target.exists():
        # Stap 9: "eerst controleren dat film.mkv nog niet bestaat (zo ja:
        # weigeren, want dat is een andere film of een halve vorige run)."
        raise MkvError(f"{target.name} bestaat al naast {source.name} - "
                       f"niet overschreven (een andere film, of een halve vorige run)")

    t0 = time.monotonic()

    # 1. Grootte, mode en gid van het origineel onthouden.
    st = source.stat()

    # 2. Duur van het origineel.
    orig_duration_s = _ffprobe_duration(source)

    # 3. Vrije ruimte. ZFS is copy-on-write (blueprint, Nog open §1): de mux
    #    kost tijdelijk de volledige bestandsgrootte extra, en
    #    shutil.disk_usage rapporteert de vrije ruimte van de POOL, die door
    #    een quota of reservering op de dataset kan afwijken van wat we hier
    #    echt mogen schrijven. Vandaar de marge boven op de kale bestandsgrootte.
    needed = st.st_size + 64 * 1024 * 1024
    if add_srt is not None and add_srt.exists():
        needed += add_srt.stat().st_size
    free = shutil.disk_usage(source.parent).free
    if free < needed:
        raise MkvError(
            f"te weinig vrije ruimte in {source.parent}: {free / 1_048_576:.0f} MB vrij, "
            f"minstens {needed / 1_048_576:.0f} MB nodig voor {source.name}. "
            f"origineel ongewijzigd.")

    # 4. Verweesde brokken opruimen.
    _cleanup_orphans(source.parent)

    orig_info = identify(source)
    chunk = source.parent / f".{source.name}.syncpart"

    # Alleen True gezet in het dubbele-faalpad van _atomic_replace: dan staat
    # de nieuwe inhoud misschien alleen nog in de chunk, en zou de gewone
    # opruiming hieronder het laatste spoor ervan wegvegen.
    skip_chunk_cleanup = False
    try:
        # 5 (muxen) + 6a-6e (controleren): allebei in _mux_to_chunk, want dat
        # is precies het stuk dat stap 5 van de blueprint apart test.
        _mux_to_chunk(source, chunk, drop_track_ids, add_srt, label, orig_info,
                     orig_duration_s, cache_dir, job)

        # 7. Rechten gelijkzetten.
        job["phase"] = "vervangen"
        try:
            os.chmod(chunk, stat.S_IMODE(st.st_mode))
        except OSError as e:
            raise MkvError(f"rechten zetten op het brok voor {target.name} mislukte: "
                           f"{e}. origineel ongewijzigd.") from e
        try:
            os.chown(chunk, -1, st.st_gid)
        except OSError as e:
            # Op de share bepaalt de mount het eigendom (blueprint, Aannames
            # §3); dat chown hier weigert is dan normaal, geen fout.
            log.debug("chown van het brok voor %s mislukt (normaal op een "
                     "gemounte share waar de mount het eigendom bepaalt): %s",
                     target.name, e)

        # 8. Atomair op zijn plaats zetten. Bij een ANDERE doelnaam (is_rename)
        # eerst nogmaals controleren dat target nog steeds niet bestaat
        # (keuringsbevinding MATIG 2): de controle bij binnenkomst van deze
        # functie was een momentopname, en tussen dat moment en hier lag de
        # hele mux - ruim genoeg tijd voor iets buiten deze dienst om alsnog
        # naar dezelfde share te schrijven. os.replace() vervangt een
        # bestaand doel stilzwijgend, dus zonder deze herhaalde controle zou
        # zo'n ondertussen verschenen bestand geruisloos overschreven worden.
        # Bij een GEWONE vervanging (film.mkv -> film.mkv, is_rename is
        # False) hoort target juist wél al te bestaan - dat IS het bestand
        # dat stap 6/7 net gecontroleerd en klaargezet hebben om te
        # vervangen - dus deze controle geldt uitdrukkelijk alleen voor het
        # pad met een andere doelnaam, anders zou elke gewone vervanging
        # voortaan zichzelf weigeren.
        if is_rename and target.exists():
            # Chunk gewoon door de finally hieronder laten opruimen: het
            # origineel (source) is op dit punt nog volledig onaangeroerd,
            # er is niets te redden zoals bij MkvCatastrophic hieronder.
            raise MkvError(
                f"{target.name} is tijdens het muxen alsnog verschenen naast "
                f"{source.name} - niet overschreven (een andere film, of een "
                f"halve vorige run). origineel ongewijzigd.")

        chunk_size = chunk.stat().st_size
        try:
            _atomic_replace(chunk, target)
        except MkvCatastrophic:
            skip_chunk_cleanup = True
            raise

        # 9. Bij een andere doelnaam: het bronbestand weg.
        if is_rename:
            try:
                source.unlink()
            except OSError as e:
                log.error(
                    "%s is aangemaakt maar %s kon niet verwijderd worden (%s) - "
                    "Jellyfin kan nu beide tonen.", target, source, e)
                raise MkvError(
                    f"{target.name} is aangemaakt maar {source.name} kon niet "
                    f"verwijderd worden: {e} - beide bestanden staan nu naast "
                    f"elkaar, met de hand opruimen.") from e

        elapsed = time.monotonic() - t0
        log.info("vervangen: %s, %.1f MB -> %.1f MB, %.0f s",
                 target, st.st_size / 1_048_576, chunk_size / 1_048_576, elapsed)
    finally:
        # 10. Het brok verwijderen als het er nog ligt; het cache-srt'je laten
        #     staan (goedkoop, wordt 's nachts opgeruimd).
        if not skip_chunk_cleanup:
            chunk.unlink(missing_ok=True)


def replace_subtitle(path: Path, track_id: int, cues: List[dict], cache_dir: Path,
                     language: Optional[str] = None, job: Optional[dict] = None) -> dict:
    """Vervangt tekstspoor track_id in path door cues (kind "replace-sub").

    language=None laat taal, spoornaam en de default/forced/hearing-impaired-
    vlaggen van het OUDE spoor ongemoeid; wordt er wel een taal opgegeven, dan
    verandert alleen --language (blueprint §3b, "Bestaand spoor vervangen":
    "Wijzig je hem, dan verandert alleen --language; de default- en
    forced-vlaggen blijven die van het oude spoor").
    """
    if job is None:
        job = _new_job("replace-sub", path)

    info = identify(path)
    track = next((t for t in info["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise MkvError(f"spoor {track_id} bestaat niet (meer) in {path.name}")
    if track["editable"] is False:
        raise MkvError(f"spoor {track_id} in {path.name} is niet bewerkbaar: "
                       f"{track['reason'] or 'onbekende reden'}")

    lang_code = track["language"]
    if language is not None:
        norm = lang.normalize(language)
        if norm is None:
            # De tekst zelf niet in de foutmelding herhalen (blueprint,
            # app.py §4): een verzoek mag geen tekst in het journaal
            # terugkaatsen, alleen de lengte.
            raise MkvError(f"onbekende taalcode aangeleverd (lengte {len(str(language))})")
        lang_code = norm

    label = {
        "language": lang_code,
        "track_name": track["name"],
        "default": track["default"],
        "forced": track["forced"],
        "hearing_impaired": track["hearing_impaired"],
    }

    # De srt die de mux in gaat wordt altijd door onszelf geschreven met
    # render_srt (blueprint §3, "Muxen") - dat lost de tekensetvraag in één
    # keer op en klapt lege regels binnen een cue in. <jobid>.srt is exact de
    # naam uit het voorbeeldcommando in de blueprint.
    srt_dir = cache_dir / "embedded"
    srt_dir.mkdir(parents=True, exist_ok=True)
    add_srt = srt_dir / f"{job['id']}.srt"
    add_srt.write_text(render_srt(cues, source=str(path)), encoding="utf-8")

    log.info("vervangen gestart: %s spoor %d, %d cues, taal %s%s",
             path.name, track_id, len(cues), lang_code,
             "" if language is None else " (opgegeven)")

    _execute_transaction(path, path, [track_id], add_srt, label, cache_dir, job)
    return {"path": str(path), "track_id": track_id, "language": lang_code}


def submit_replace_subtitle(path: Path, track_id: int, cues: List[dict],
                            cache_dir: Path, language: Optional[str] = None) -> dict:
    """Start het vervangen van een tekstspoor als achtergrondtaak. Zelfde
    grendel als submit_extract(): hoogstens één taak tegelijk, van welke soort
    ook (blueprint §3, "Taken")."""
    # Zelfde synchrone controle als submit_extract() vóór het starten van de
    # achtergrondtaak: track_id en taal zijn invoer van buiten (blueprint §4,
    # Veiligheid) en horen een directe fout te geven aan de aanroeper van dit
    # verzoek, niet pas zichtbaar te worden als een taak die een seconde later
    # op "error" springt. replace_subtitle() doet dezelfde identify()-aanroep
    # opnieuw in de achtergrondthread - dat is bewust (zelfde dubbele controle
    # als extract()/submit_extract() al hebben): tussen het indienen en het
    # echt starten kan het bestand gewijzigd zijn, dus de thread vertrouwt niet
    # blind op wat hier al gecontroleerd is.
    info = identify(path)
    track = next((t for t in info["tracks"] if t["id"] == track_id), None)
    if track is None:
        raise MkvError(f"spoor {track_id} bestaat niet (meer) in {path.name}")
    if track["editable"] is False:
        raise MkvError(f"spoor {track_id} in {path.name} is niet bewerkbaar: "
                       f"{track['reason'] or 'onbekende reden'}")
    if language is not None and lang.normalize(language) is None:
        # De tekst zelf niet in de foutmelding herhalen (blueprint §4: "de
        # tekst uit het verzoek wordt daarbij niet in de foutmelding herhaald,
        # alleen de lengte").
        raise MkvError(f"onbekende taalcode aangeleverd (lengte {len(str(language))})")

    global _CURRENT
    # Zelfde opzet als submit_extract(): file-lock verkrijgen binnen dezelfde
    # with _LOCK-sectie als de in-memory controle, zodat "is er al iets
    # bezig" één atomaire beslissing blijft (keuringsbevinding MATIG 5).
    file_lock = _FileLock(cache_dir)
    with _LOCK:
        if _CURRENT is not None and _CURRENT["state"] in JOB_STATES_ACTIVE:
            raise MkvBusy(_CURRENT["path"])
        file_lock.acquire(str(path))
        job = _new_job("replace-sub", path)
        _CURRENT = job

    threading.Thread(target=_run_replace_job,
                     args=(job, path, track_id, cues, cache_dir, language, file_lock),
                     daemon=True, name=f"mkv-replace-{job['id'][:8]}").start()
    return dict(job)


def _run_replace_job(job: dict, path: Path, track_id: int, cues: List[dict],
                     cache_dir: Path, language: Optional[str],
                     file_lock: "_FileLock") -> None:
    global _CURRENT
    job["state"] = "running"
    job["phase"] = "voorbereiden"
    try:
        result = replace_subtitle(path, track_id, cues, cache_dir,
                                  language=language, job=job)
        job["percent"] = 100
        job["phase"] = None
        job["state"] = "done"
        job["finished"] = _now()
        job["result"] = result
        log.info("vervangen klaar: %s spoor %s", path.name, track_id)
    except MkvError as e:
        job["state"] = "error"
        job["error"] = str(e)
        log.error("vervangen mislukt (fase %s) op %s spoor %s: %s",
                 job.get("phase"), path.name, track_id, e)
        job["phase"] = None
        job["finished"] = _now()
    except Exception as e:
        # Zelfde afspraak als _run_extract_job: een bug mag de taak niet
        # stilzwijgend op "running" laten hangen.
        job["state"] = "error"
        job["error"] = f"onverwachte fout: {e}"
        job["phase"] = None
        job["finished"] = _now()
        log.exception("vervangen: onverwachte fout op %s spoor %s", path.name, track_id)
    finally:
        file_lock.release()
        with _LOCK:
            if _CURRENT is job:
                _CURRENT = None
            _DONE.append(job)


# ---------------------------------------------------------------- ingest

def _same_sub_key(existing_lang: str, existing_forced: bool,
                  new_lang: str, new_forced: bool) -> bool:
    """De overslaan-sleutel uit blueprint §3, "Idempotentie": genormaliseerde
    taalcode + forced-vlag. "und" bewijst nooit dat het juiste spoor er al
    staat ("we weten het niet, geen bewijs"), dus telt nooit als gelijk - ook
    niet und tegen und, anders zou een tweede --lang und-run zichzelf
    blokkeren terwijl dat juist een bewuste, herhaalbare keuze moet blijven.
    """
    if existing_lang == "und" or new_lang == "und":
        return False
    return existing_lang == new_lang and bool(existing_forced) == bool(new_forced)


def _resolve_ingest(video: Path, srt: Path, language: Optional[str], force: bool) -> dict:
    """Bepaalt taal, vlaggen, doelpad en of dit werk al gedaan is.

    Gedeeld tussen submit_ingest() (snelle synchrone voorcontrole, vóór er een
    taak/grendel is) en ingest() zelf (nog een keer, in de achtergrondthread -
    zelfde dubbele controle als submit_extract()/submit_replace_subtitle() al
    hebben, want tussen het indienen en het echt starten kan het bestand
    gewijzigd zijn). Roept identify() aan (een mkvmerge -J-subprocess), dus
    bewust GEEN cache: het bestand kan intussen veranderd zijn.

    Geeft {label, target, is_rename, skip, tag_info}. `skip` is niet None als
    het doel al een tekstondertitelspoor met dezelfde (taal, forced)-sleutel
    heeft (blueprint §3, Idempotentie) - dan is er niets te muxen.

    Kan MkvError (onbekende opgelegde taalcode) of MkvLanguageUnknown
    (blueprint §3b: "web vraagt") opgooien.
    """
    # De taal komt uit het achtervoegsel van de srt-bestandsnaam, dezelfde
    # ontleding als find_srts() gebruikt (blueprint §3b: "Losse srt in een
    # bestaande MKV" / "AVI/MP4 omzetten" - één functie, dezelfde regels).
    tag_info = lang.detect_srt(video.stem, srt.name)
    forced = bool(tag_info.get("forced"))
    hearing_impaired = bool(tag_info.get("hearing_impaired"))

    if language is not None:
        lang_code = lang.normalize(language)
        if lang_code is None:
            # De tekst zelf niet in de foutmelding herhalen (blueprint §4,
            # Veiligheid: "de tekst uit het verzoek wordt daarbij niet in de
            # foutmelding herhaald, alleen de lengte").
            raise MkvError(f"onbekende taalcode aangeleverd (lengte {len(str(language))})")
        # Geen log.info hier (keuringsbevinding KLEIN 3): deze functie draait
        # bewust twee keer per ingest-poging (submit_ingest()'s voorcontrole
        # ÉN ingest()'s eigen aanroep in de achtergrondthread, zie de
        # docstring hierboven) - een onvoorwaardelijke regel hier zou dus
        # twee keer in het journaal staan voor elke geslaagde ingest. De taal
        # zit al in het teruggegeven `label`/`tag_info`; _log_ingest_language()
        # hieronder logt hem precies één keer, op het punt waar de uitkomst
        # ook echt telt.
    elif tag_info["language"]:
        lang_code = tag_info["language"]
    else:
        # Deze tak eindigt altijd in een raise: welke van de twee aanroepen
        # ('m submit_ingest() of ingest()) hier ook terechtkomt, dat IS de
        # enige keer dat dit verzoek/deze poging ooit verder komt dan hier -
        # dus wél onvoorwaardelijk loggen. Dat verdubbelt nooit, want de
        # tweede aanroep (in ingest()) gebeurt alleen als de eerste geen
        # MkvLanguageUnknown opgooide.
        log.info("taal onbepaald (%s, %s)", srt.name, tag_info["origin_text"])
        raise MkvLanguageUnknown(tag_info["tag"], tag_info["source"])

    info = identify(video)
    # "Beeldsporen (PGS, VobSub) tellen niet mee in deze controle" (blueprint
    # §3, Idempotentie) - editable is False voor precies die twee codecs, dus
    # "editable is not False" is tekstspoor, bewerkbaar of niet (ASS/SSA telt
    # wél mee: dat IS een tekstspoor, het kan alleen niet rechtstreeks bewerkt
    # worden).
    existing_text = [t for t in info["tracks"]
                     if t["type"] == "subtitles" and t["editable"] is not False]

    skip = None
    if not force:
        for t in existing_text:
            if _same_sub_key(t["language"], t["forced"], lang_code, forced):
                skip = {
                    "path": str(video), "language": lang_code, "forced": forced,
                    "reason": f"{video.name} heeft al een tekstspoor met taal "
                             f"{lang_code}{' (forced)' if forced else ''}",
                }
                break

    # Beslist in ronde 2, §3: default aan zolang er nog GEEN ander tekstspoor
    # is, en NOOIT bij forced - ook niet als het toevallig het eerste spoor
    # is, want een geforceerd spoor toont alleen de anderstalige stukken en is
    # dus nooit bedoeld als het spoor dat standaard aanstaat.
    default_flag = (not existing_text) and not forced

    # Embedden (al een MKV) laat de naam en extensie ongemoeid; omzetten
    # (AVI/MP4) levert altijd .mkv op - blueprint §3, "Muxen": "Een AVI
    # omzetten is hetzelfde met film.avi als eerste invoer."
    target = video if video.suffix.lower() == ".mkv" else video.with_suffix(".mkv")

    label = {
        "language": lang_code,
        # display() en niet de bestandsnaam (blueprint, Veiligheid: "de
        # spoornaam wordt evenmin uit de bestandsnaam overgenomen").
        "track_name": lang.display(lang_code),
        "default": default_flag,
        "forced": forced,
        "hearing_impaired": hearing_impaired,
    }
    return {"label": label, "target": target, "is_rename": target != video,
            "skip": skip, "tag_info": tag_info}


def _log_ingest_language(language: Optional[str], plan: dict, srt: Path) -> None:
    """Eén regel taalbepaling voor een ingest-poging (blueprint, Werking bij
    fouten: "INFO bij elke taalbepaling, één regel").

    Bewust GEEN onderdeel van _resolve_ingest() zelf (keuringsbevinding
    KLEIN 3): die functie draait twee keer per poging (submit_ingest()'s
    voorcontrole én ingest()'s eigen, latere aanroep in de achtergrondthread -
    hetzelfde dubbele-check-patroon als submit_extract()/
    submit_replace_subtitle() al hebben, en dat blijft, want tussen indienen
    en echt starten kan het bestand gewijzigd zijn). Loggen bij elke aanroep
    zou dus voor elke geslaagde ingest twee identieke regels opleveren. In
    plaats daarvan roepen de twee plekken die een uitkomst ook echt AFHANDELEN
    dit hier aan: submit_ingest() voor een skip (geen taak start, dus geen
    tweede _resolve_ingest()-aanroep die het alsnog zou doen) en ingest() voor
    al het andere (dat IS "het punt waar de taak ook echt start" - zowel bij
    een taak uit de webweg als bij een rechtstreekse aanroep vanuit
    convert.py, dat geen submit_ingest()-voorcontrole heeft).

    `language` is de oorspronkelijk aangeleverde taalcode (None = uit de
    bestandsnaam gehaald); `plan` is het resultaat van _resolve_ingest().
    """
    lang_code = plan["label"]["language"]
    tag_info = plan["tag_info"]
    if language is not None:
        log.info("taal %s opgelegd (%s, %s)", lang_code, srt.name, tag_info["origin_text"])
    else:
        log.info("taal %s %s (%s)", lang_code, tag_info["origin_text"], srt.name)


def ingest(video: Path, srt: Path, cache_dir: Path, language: Optional[str] = None,
          drop_srt: bool = False, force: bool = False, job: Optional[dict] = None) -> dict:
    """Bouwt een MKV met srt erin - hetzij een bestaand MKV waar de srt bij
    komt, hetzij een AVI/MP4 dat wordt omgezet (blueprint §3b: "één handeling,
    niet twee" - convert.py roept straks exact deze functie aan, dus alle
    regels (taalbepaling, idempotentie, default-vlag) staan hier één keer.

    `force=True` zet de overslaan-controle uit (blueprint §3, Idempotentie:
    "--force in de CLI zet de hele controle uit") - nog niet aangeroepen
    vanuit de webweg of app.py, maar convert.py (stap 9, latere stap) heeft
    hem nodig en moet dan niet nog een keer deze functie moeten aanpassen.
    """
    if job is None:
        job = _new_job("ingest", video)

    plan = _resolve_ingest(video, srt, language, force)
    # Hier loggen, niet in _resolve_ingest() zelf (keuringsbevinding KLEIN 3):
    # dit is "het punt waar de taak ook echt start" - hetzij als achtergrond-
    # taak vanuit _run_ingest_job(), hetzij rechtstreeks vanuit convert.py
    # (stap 9), dat geen submit_ingest()-voorcontrole gebruikt en dus zonder
    # deze regel hier helemaal geen taal-log zou krijgen.
    _log_ingest_language(language, plan, srt)
    if plan["skip"] is not None:
        # Blueprint, Stappen voor de implementer, controle (b): "Twee keer
        # draaien geeft de tweede keer 'al gedaan' en geen tweede spoor." Geen
        # mkvmerge-aanroep, geen brok, geen wijziging - alleen een taak die
        # meteen "done" is met het resultaat erin.
        log.info("ingest overgeslagen: %s", plan["skip"]["reason"])
        job["state"] = "done"
        job["percent"] = 100
        job["result"] = {**plan["skip"], "skipped": True}
        job["finished"] = _now()
        return job["result"]

    label = plan["label"]
    target = plan["target"]

    # De srt die de mux in gaat wordt altijd door onszelf geschreven met
    # render_srt (blueprint §3, "Muxen"): lost de tekensetvraag in één keer op
    # (read_text snuffelt de codering, render_srt schrijft UTF-8) en klapt
    # lege regels binnen een cue in - ook al ligt er al een .srt op schijf.
    srt_dir = cache_dir / "embedded"
    srt_dir.mkdir(parents=True, exist_ok=True)
    add_srt = srt_dir / f"{job['id']}.srt"
    cues = parse_srt(read_text(srt), source=str(srt))
    add_srt.write_text(render_srt(cues, source=str(srt)), encoding="utf-8")

    log.info("ingest gestart: %s + %s -> %s, %d cues, taal %s%s%s",
             video.name, srt.name, target.name, len(cues), label["language"],
             " forced" if label["forced"] else "", " default" if label["default"] else "")

    _execute_transaction(video, target, [], add_srt, label, cache_dir, job)

    if drop_srt:
        try:
            srt.unlink()
        except OSError as e:
            # De inbouw is al geslaagd op dit punt (_execute_transaction is al
            # terug zonder fout) - het opruimen van de losse srt mag dat
            # succes niet alsnog in een MkvError veranderen. Blueprint §3b,
            # Beslist ronde 2 §2: de srt blijft standaard toch al staan, dus
            # dit is puur de opgeruimde variant die hier niet lukte.
            log.warning("losse srt %s kon niet verwijderd worden na het inbouwen "
                       "(het spoor zelf is wel toegevoegd): %s", srt, e)

    return {"path": str(target), "language": label["language"], "forced": label["forced"],
           "default": label["default"], "renamed_from": str(video) if plan["is_rename"] else None}


def submit_ingest(video: Path, srt: Path, cache_dir: Path, language: Optional[str] = None,
                  drop_srt: bool = False, force: bool = False) -> dict:
    """Start het inbouwen/omzetten als achtergrondtaak. Zelfde grendel als
    submit_extract()/submit_replace_subtitle(): hoogstens één taak tegelijk,
    van welke soort ook (blueprint §3, "Taken").

    _resolve_ingest() hier vooraf (buiten de grendel, want identify() is een
    subprocess-aanroep die niet hoeft te wachten op de lock) geeft een directe
    fout aan de aanroeper van dit verzoek bij een onbekende taalcode of een
    onbepaalde taal, in plaats van dat pas zichtbaar te maken via een taak die
    een fase later op "error" springt (zelfde afspraak als de andere
    submit_*-functies).

    Deze voorcontrole logt de taalbepaling zelf NIET (keuringsbevinding
    KLEIN 3): gaat er straks een achtergrondtaak lopen, dan doet ingest()'s
    eigen, latere _resolve_ingest()-aanroep dat al - loggen op allebei de
    plekken gaf voor elke geslaagde ingest twee identieke regels. Alleen bij
    een skip hieronder (geen taak, dus geen tweede aanroep die het alsnog zou
    doen) is deze voorcontrole de volledige afhandeling en logt hij wél.
    """
    plan = _resolve_ingest(video, srt, language, force)

    global _CURRENT
    file_lock = _FileLock(cache_dir)
    with _LOCK:
        job = _new_job("ingest", video)
        if plan["skip"] is None:
            # Alleen de grendel controleren/pakken voor een taak die ook echt
            # gaat muxen - een taak die toch al "al gedaan" is zou anders
            # onnodig een MkvBusy krijgen op een taak die er niets mee te
            # maken heeft, terwijl er in werkelijkheid niets loopt te wijzigen.
            if _CURRENT is not None and _CURRENT["state"] in JOB_STATES_ACTIVE:
                raise MkvBusy(_CURRENT["path"])
            file_lock.acquire(str(video))
            _CURRENT = job

    if plan["skip"] is not None:
        # Hier WEL loggen, in tegenstelling tot de docstring-afspraak
        # hierboven voor het proceed-pad: bij een skip start er geen
        # achtergrondtaak en dus ook geen tweede _resolve_ingest()-aanroep die
        # de taal alsnog zou loggen (keuringsbevinding KLEIN 3) - deze
        # voorcontrole IS de volledige afhandeling van deze poging.
        _log_ingest_language(language, plan, srt)
        log.info("ingest overgeslagen: %s", plan["skip"]["reason"])
        job["state"] = "done"
        job["percent"] = 100
        job["result"] = {**plan["skip"], "skipped": True}
        job["finished"] = _now()
        with _LOCK:
            _DONE.append(job)
        return dict(job)

    threading.Thread(target=_run_ingest_job,
                     args=(job, video, srt, cache_dir, language, drop_srt, force, file_lock),
                     daemon=True, name=f"mkv-ingest-{job['id'][:8]}").start()
    return dict(job)


def _run_ingest_job(job: dict, video: Path, srt: Path, cache_dir: Path,
                    language: Optional[str], drop_srt: bool, force: bool,
                    file_lock: "_FileLock") -> None:
    global _CURRENT
    job["state"] = "running"
    job["phase"] = "voorbereiden"
    try:
        result = ingest(video, srt, cache_dir, language=language, drop_srt=drop_srt,
                        force=force, job=job)
        job["percent"] = 100
        job["phase"] = None
        job["state"] = "done"
        job["finished"] = _now()
        job["result"] = result
        log.info("ingest klaar: %s", result.get("path"))
    except MkvError as e:
        job["state"] = "error"
        job["error"] = str(e)
        log.error("ingest mislukt (fase %s) op %s: %s", job.get("phase"), video.name, e)
        job["phase"] = None
        job["finished"] = _now()
    except Exception as e:
        # Zelfde afspraak als _run_extract_job/_run_replace_job: een bug mag
        # de taak niet stilzwijgend op "running" laten hangen.
        job["state"] = "error"
        job["error"] = f"onverwachte fout: {e}"
        job["phase"] = None
        job["finished"] = _now()
        log.exception("ingest: onverwachte fout op %s", video.name)
    finally:
        file_lock.release()
        with _LOCK:
            if _CURRENT is job:
                _CURRENT = None
            _DONE.append(job)
