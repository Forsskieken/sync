#!/usr/bin/env python3
"""
sync - ondertitels bijregelen vanuit de zetel.

Leest de actieve Jellyfin-sessie, laadt de bijhorende .srt van schijf,
schrijft aangepaste tijden terug, en laat de speler het spoor herladen.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import lang
import mkv
from paths import allowed_roots, resolve_in_roots
from subs import find_srts, parse_srt, read_text, write_srt

# ---------------------------------------------------------------- logging

# Naar stderr, want journald vangt dat op: journalctl -u subtitle-sync -f.
# LOG_LEVEL bestaat omdat de DEBUG-regels van find_srts ("waarom staat mijn srt
# niet in de lijst") anders nooit te zien zijn; standaard blijft het INFO.
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_want = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# Het niveau gaat op onze eigen loggers, niet op de root. httpx logt namelijk
# elk voltooid verzoek op INFO, en de pagina pollt /api/session elke seconde:
# met INFO op de root zijn dat ~86 000 regels per dag waarin onze eigen regels
# verdrinken, en met DEBUG komen httpcore en asyncio er nog bij. De root blijft
# daarom op WARNING staan - waarschuwingen en fouten uit bibliotheken wil je wél
# zien, alleen hun gebabbel niet.
_OWN = ("sync", "subs", "paths", "lang", "mkv", "convert")
logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)s %(name)s %(message)s")
for _name in _OWN:
    logging.getLogger(_name).setLevel(_want if _want in _LEVELS else "INFO")
log = logging.getLogger("sync")
if _want not in _LEVELS:
    # Afgekapt: de waarde komt uit de omgeving en een onzinwaarde van een
    # kilobyte hoort niet ongefilterd in het journaal te belanden.
    log.warning("LOG_LEVEL %r is onbekend, INFO gebruikt", _want[:20])

# ---------------------------------------------------------------- config

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin.example.net:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

# Jellyfin ziet /mnt/jellyfin/Serie/..., deze host ziet /mnt/Serie/...
PATH_MAP = json.loads(os.environ.get("PATH_MAP", '{"/mnt/jellyfin": "/mnt"}'))

# Buiten deze mappen wordt niets gelezen of geschreven.
ALLOWED_ROOTS = allowed_roots()

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/subtitle-sync"))
HERE = Path(__file__).parent

# ------------------------------------------------------------ taalknoppen

# Standaard nld/eng (blueprint §3b, "De configuratie"): de collectie is
# grotendeels Nederlands, soms Engels. Puur een bedieningsvoorkeur - bepaalt
# alleen welke twee tegels de pagina meteen toont, labelt zelf nooit iets.
_SUB_LANG_BUTTONS_DEFAULT = ["nld", "eng"]

# Zes vaste tegels voor de tweede taalrij ("Andere…", blueprint §3b). Deze
# lijst wisselt nooit mee met SUB_LANG_BUTTONS - letterlijk de zes codes uit
# de blueprinttekst, zodat een admin die de eerste rij aanpast niet ook nog
# de "vaste" rij kan laten verschuiven.
_LANG_MORE_CODES = ["fra", "deu", "spa", "ita", "por", "und"]


def _load_sub_lang_buttons() -> List[str]:
    """SUB_LANG_BUTTONS uit de omgeving: welke taalcodes de eerste knoppenrij
    toont. Een lege, ongeldige of onbekende waarde valt terug op de standaard
    met een WARN-regel (blueprint §3b): een verkeerde waarde hier mag nooit
    een taal in stilte fout labelen, dus liever de veilige standaard dan een
    onbruikbare configuratie laten doorwerken.
    """
    raw = os.environ.get("SUB_LANG_BUTTONS", "").strip()
    if not raw:
        return list(_SUB_LANG_BUTTONS_DEFAULT)
    try:
        value = json.loads(raw)
        if not isinstance(value, list) or not value:
            raise ValueError("moet een niet-lege JSON-lijst zijn")
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("SUB_LANG_BUTTONS %r is ongeldig (%s), standaard %s gebruikt",
                   raw[:200], e, _SUB_LANG_BUTTONS_DEFAULT)
        return list(_SUB_LANG_BUTTONS_DEFAULT)
    codes = []
    for item in value:
        norm = lang.normalize(item) if isinstance(item, str) else None
        if norm is None:
            log.warning("SUB_LANG_BUTTONS bevat een onbekende taalcode %r, overgeslagen", item)
            continue
        codes.append(norm)
    if not codes:
        log.warning("SUB_LANG_BUTTONS leverde geen enkele geldige taalcode op, "
                   "standaard %s gebruikt", _SUB_LANG_BUTTONS_DEFAULT)
        return list(_SUB_LANG_BUTTONS_DEFAULT)
    return codes


SUB_LANG_BUTTONS = _load_sub_lang_buttons()


def _lang_tiles(codes: List[str]) -> List[dict]:
    """[{code, display}, ...] - display() is de enige bron voor de naam, dus
    de voorkant hoeft de taaltabel niet in JavaScript te dupliceren."""
    return [{"code": c, "display": lang.display(c)} for c in codes]

# Chrome speelt deze audiocodecs af; al de rest moet naar AAC.
PLAYABLE = {"aac", "mp3", "opus", "vorbis", "flac"}

app = FastAPI(title="sync")

# ---------------------------------------------------------------- paden

def safe_path(p: str) -> Path:
    """resolve_in_roots met de HTTP-fout eromheen; de voorkant merkt niets."""
    q = resolve_in_roots(p, ALLOWED_ROOTS)
    if q is None:
        raise HTTPException(403, f"Pad ligt buiten de toegelaten mappen: {p}")
    return q


def map_path(jf_path: str) -> str:
    for src, dst in PATH_MAP.items():
        if jf_path.startswith(src):
            return dst + jf_path[len(src):]
    return jf_path

# ---------------------------------------------------------------- jellyfin

async def jf_get(path: str, params: Optional[dict] = None):
    if not JELLYFIN_API_KEY:
        raise HTTPException(500, "JELLYFIN_API_KEY is niet ingesteld")
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{JELLYFIN_URL}{path}", params=params,
                        headers={"X-Emby-Token": JELLYFIN_API_KEY})
        r.raise_for_status()
        return r.json()


async def jf_post(path: str, params: Optional[dict] = None, body: Optional[dict] = None):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{JELLYFIN_URL}{path}", params=params, json=body,
                         headers={"X-Emby-Token": JELLYFIN_API_KEY})
        r.raise_for_status()
        return True

# ---------------------------------------------------------------- api

@app.get("/")
def index():
    return FileResponse(HERE / "sync.html")


@app.get("/api/session")
async def session():
    sessions = await jf_get("/Sessions")
    playing = [s for s in sessions if s.get("NowPlayingItem")]
    if not playing:
        return {"playing": False}

    s = playing[0]
    item = s["NowPlayingItem"]
    state = s.get("PlayState") or {}

    jf_path = item.get("Path") or ""
    local = map_path(jf_path)

    name = item.get("Name") or ""
    if item.get("SeriesName"):
        se = f"S{item.get('ParentIndexNumber', 0):02d}E{item.get('IndexNumber', 0):02d}"
        name = f"{item['SeriesName']} {se} - {name}"

    subs = []
    try:
        subs = find_srts(safe_path(local))
    except HTTPException:
        pass

    return {
        "playing": True,
        "session_id": s.get("Id"),
        "device": s.get("DeviceName") or s.get("Client"),
        "item_id": item.get("Id"),
        "name": name,
        "position_ms": int((state.get("PositionTicks") or 0) / 10_000),
        "runtime_ms": int((item.get("RunTimeTicks") or 0) / 10_000),
        "paused": bool(state.get("IsPaused")),
        "subtitle_index": state.get("SubtitleStreamIndex"),
        "video_path": local,
        "subtitles": subs,
    }


@app.get("/api/subtitle")
def subtitle(path: str):
    f = safe_path(path)
    if not f.is_file():
        raise HTTPException(404, f"Niet gevonden: {path}")
    # Het pad meegeven zodat de WARN-regel over weggegooide blokken zegt om
    # welk bestand het gaat.
    cues = parse_srt(read_text(f), source=str(f))
    return {"path": str(f), "name": f.name, "cues": cues}


class Cue(BaseModel):
    start: int
    end: int
    text: str


class SaveBody(BaseModel):
    path: str
    cues: List[Cue]


@app.post("/api/subtitle/save")
def subtitle_save(body: SaveBody):
    f = safe_path(body.path)
    if not f.is_file():
        raise HTTPException(404, f"Niet gevonden: {body.path}")
    cues = sorted((c.model_dump() for c in body.cues), key=lambda c: c["start"])
    backup = write_srt(f, cues)
    return {"saved": str(f), "backup": str(backup), "cues": len(cues)}


@app.get("/api/browse")
def browse(path: str):
    """Bladeren door de toegelaten mappen, voor de lokale modus."""
    d = safe_path(path)
    if not d.is_dir():
        raise HTTPException(400, f"Geen map: {path}")
    dirs, vids = [], []
    for f in sorted(d.iterdir()):
        if f.name.startswith("."):
            continue
        if f.is_dir():
            dirs.append({"name": f.name, "path": str(f)})
        elif f.suffix.lower() in mkv.VIDEO_EXTENSIONS:
            vids.append({"name": f.name, "path": str(f), "subtitles": find_srts(f)})
    return {"path": str(d), "parent": str(d.parent), "dirs": dirs, "videos": vids}

# ---------------------------------------------------------------- mkv

def _video_path(path: str) -> Path:
    """safe_path plus de containercontrole die identify()/extract() nodig
    hebben - dezelfde extensielijst als browse() (blueprint, Veiligheid:
    "alleen bekende extensies")."""
    f = safe_path(path)
    if not f.is_file():
        raise HTTPException(404, f"Niet gevonden: {path}")
    if f.suffix.lower() not in mkv.VIDEO_EXTENSIONS:
        raise HTTPException(400, f"Geen ondersteund videobestand: {path}")
    return f


@app.get("/api/mkv/tracks")
def mkv_tracks(path: str):
    f = _video_path(path)
    try:
        info = mkv.identify(f)
    except mkv.MkvError as e:
        # Hier altijd 500: dit endpoint doet niets anders dan mkvmerge -J
        # aanroepen en teruggeven, dus een fout hier is een falend extern
        # programma, geen afgekeurde invoer - zelfde afspraak als probe()
        # hierboven bij ffprobe.
        raise HTTPException(500, str(e))
    # De taaltegels meesturen met de sporenlijst in plaats van er een zesde
    # endpoint voor te maken: de voorkant vraagt dit toch alleen op het moment
    # dat ze de taalkeuze voor dit bestand moet tekenen (blueprint §3b, "De
    # voorkant").
    info["lang_buttons"] = _lang_tiles(SUB_LANG_BUTTONS)
    info["lang_more"] = _lang_tiles(_LANG_MORE_CODES)
    return info


class ExtractBody(BaseModel):
    path: str
    track_id: int


@app.post("/api/mkv/extract")
def mkv_extract(body: ExtractBody):
    f = _video_path(body.path)
    try:
        job = mkv.submit_extract(f, body.track_id, CACHE_DIR)
    except mkv.MkvBusy as e:
        raise HTTPException(409, str(e))
    except mkv.MkvError as e:
        # track_id bestaat niet in dit bestand, of het spoor is niet
        # bewerkbaar (PGS/VobSub): dat is afgekeurde invoer, geen tool-fout.
        raise HTTPException(400, str(e))
    return {"job": job}


@app.get("/api/mkv/job")
def mkv_job(id: str):
    job = mkv.get_job(id)
    if job is None:
        raise HTTPException(404, "Onbekende taak")
    return job


class SaveMkvBody(BaseModel):
    path: str
    track_id: int
    cues: List[Cue]
    # Ontbreekt dit veld, dan laat mkv.replace_subtitle taal, spoornaam en de
    # default/forced/hearing-impaired-vlaggen van het OUDE spoor ongemoeid
    # (blueprint §4: "language ontbreekt = taal en vlaggen van het oude spoor
    # behouden").
    language: Optional[str] = None


@app.post("/api/mkv/save")
def mkv_save(body: SaveMkvBody):
    f = _video_path(body.path)
    # Zelfde sortering als /api/subtitle/save: de cues gaan in tijdsorde de
    # mux in, ongeacht in welke volgorde de voorkant ze aanleverde.
    cues = sorted((c.model_dump() for c in body.cues), key=lambda c: c["start"])
    try:
        job = mkv.submit_replace_subtitle(f, body.track_id, cues, CACHE_DIR,
                                          language=body.language)
    except mkv.MkvBusy as e:
        raise HTTPException(409, str(e))
    except mkv.MkvError as e:
        # track_id die niet bestaat, een niet-bewerkbaar spoor (PGS/VobSub) of
        # een taalcode die lang.normalize() niet kent: alle drie afgekeurde
        # invoer voor DIT verzoek, dus 400 - submit_replace_subtitle() heeft
        # dat al synchroon gecontroleerd vóór de achtergrondtaak start
        # (blueprint §4, Veiligheid).
        raise HTTPException(400, str(e))
    return {"job": job}


def _srt_path(path: str) -> Path:
    """safe_path plus de extensiecontrole voor een losse ondertitel - dezelfde
    soort controle als _video_path() hierboven, maar dan voor de srt-kant van
    /api/mkv/ingest (blueprint §4, §5)."""
    f = safe_path(path)
    if not f.is_file():
        raise HTTPException(404, f"Niet gevonden: {path}")
    if f.suffix.lower() != ".srt":
        raise HTTPException(400, f"Geen .srt-bestand: {path}")
    return f


class IngestBody(BaseModel):
    video_path: str
    srt_path: str
    language: Optional[str] = None
    drop_srt: bool = False


@app.post("/api/mkv/ingest")
def mkv_ingest(body: IngestBody):
    video = _video_path(body.video_path)
    srt = _srt_path(body.srt_path)

    # Blueprint, Veiligheid: "het MKV-pad, het srt-pad, én het AFGELEIDE
    # doelpad ... worden elk apart gecontroleerd, en bovendien wordt
    # gecontroleerd dat de doelmap gelijk is aan de bronmap." mkv.ingest()
    # berekent hetzelfde doelpad (video met .mkv-extensie) zelf nog een keer
    # uit dezelfde, al gevalideerde `video` - dit is een extra, goedkope
    # verdedigingslaag hier, niet de enige controle: _execute_transaction()
    # weigert zelf ook als doel- en bronmap uiteenlopen.
    derived_target = video if video.suffix.lower() == ".mkv" else video.with_suffix(".mkv")
    target = safe_path(str(derived_target))
    if target.parent != video.parent:
        raise HTTPException(403, "Doelmap wijkt af van de bronmap")

    try:
        job = mkv.submit_ingest(video, srt, CACHE_DIR, language=body.language,
                                drop_srt=body.drop_srt)
    except mkv.MkvBusy as e:
        raise HTTPException(409, str(e))
    except mkv.MkvLanguageUnknown as e:
        # De afgesproken foutvorm (blueprint §4): de voorkant herkent "code"
        # en klapt de taaltegels open in plaats van een rode melding te tonen.
        raise HTTPException(422, {"code": "language_unknown", "tag": e.tag, "reason": e.reason})
    except mkv.MkvError as e:
        # Onbekende opgelegde taalcode: afgekeurde invoer voor DIT verzoek,
        # dus 400 - submit_ingest() heeft dat al synchroon gecontroleerd vóór
        # de achtergrondtaak start (blueprint §4, Veiligheid).
        raise HTTPException(400, str(e))
    return {"job": job}

# ---------------------------------------------------------------- audio

def probe(src: Path) -> dict:
    """Codec en duur van het eerste audiospoor."""
    r = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name:format=duration",
        "-of", "json", str(src)
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"ffprobe faalde op {src.name}")
    d = json.loads(r.stdout or "{}")
    streams = d.get("streams") or []
    if not streams:
        raise HTTPException(400, f"Geen audiospoor in {src.name}")
    codec = streams[0].get("codec_name", "")
    try:
        dur = float((d.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"codec": codec, "duration_ms": int(dur * 1000)}


def cache_key(src: Path) -> str:
    return hashlib.sha1(f"{src}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:16]


def _fps(val: str) -> Optional[float]:
    """ffprobe geeft '24000/1001'; daar willen we 23.976 uit."""
    try:
        num, den = val.split("/")
        den = float(den)
        return round(float(num) / den, 3) if den else None
    except (ValueError, ZeroDivisionError, AttributeError):
        return None


@app.get("/api/video/info")
def video_info(path: str):
    """Framerate van het videospoor - vult het doelveld van de omzetting in."""
    src = safe_path(path)
    if not src.is_file():
        raise HTTPException(404, f"Niet gevonden: {path}")
    r = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate",
        "-of", "json", str(src)
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"ffprobe faalde op {src.name}")
    streams = (json.loads(r.stdout or "{}").get("streams") or [{}])
    s = streams[0] if streams else {}
    fps = _fps(s.get("avg_frame_rate", "")) or _fps(s.get("r_frame_rate", ""))
    return {"fps": fps}


# key -> {state, percent, codec, copy, error, duration_ms}
JOBS: Dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def run_extract(key: str, src: Path, out: Path, info: dict):
    tmp = out.with_suffix(".tmp.m4a")
    can_copy = info["codec"] in PLAYABLE
    dur = max(1, info["duration_ms"])

    with JOBS_LOCK:
        JOBS[key].update(state="running", percent=0)

    cmd = ["ffmpeg", "-y", "-nostdin", "-i", str(src),
           "-vn", "-sn", "-dn", "-map", "0:a:0"]
    cmd += ["-c:a", "copy"] if can_copy else ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-loglevel", "error", str(tmp)]

    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in p.stdout:
            if line.startswith("out_time_ms="):
                try:
                    done = int(line.split("=", 1)[1]) / 1000  # microseconden -> ms
                except ValueError:
                    continue
                with JOBS_LOCK:
                    JOBS[key]["percent"] = min(99, int(done / dur * 100))
        p.wait()
        if p.returncode != 0 or not tmp.exists():
            err = (p.stderr.read() or "").strip()[-300:]
            raise RuntimeError(err or "ffmpeg gaf een fout")
        tmp.rename(out)
        with JOBS_LOCK:
            JOBS[key].update(state="done", percent=100)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with JOBS_LOCK:
            JOBS[key].update(state="error", error=str(e))


class PrepareBody(BaseModel):
    path: str


@app.post("/api/audio/prepare")
def audio_prepare(body: PrepareBody):
    src = safe_path(body.path)
    if not src.is_file():
        raise HTTPException(404, f"Niet gevonden: {body.path}")

    key = cache_key(src)
    out = CACHE_DIR / f"{key}.m4a"
    if out.exists():
        return {"key": key, "state": "done", "percent": 100, "cached": True}

    with JOBS_LOCK:
        job = JOBS.get(key)
        if job and job["state"] in ("queued", "running"):
            return {"key": key, **job}

    info = probe(src)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    can_copy = info["codec"] in PLAYABLE

    with JOBS_LOCK:
        JOBS[key] = {"state": "queued", "percent": 0, "codec": info["codec"],
                     "copy": can_copy, "error": None,
                     "duration_ms": info["duration_ms"]}

    threading.Thread(target=run_extract, args=(key, src, out, info), daemon=True).start()
    return {"key": key, "state": "queued", "percent": 0,
            "codec": info["codec"], "copy": can_copy}


@app.get("/api/audio/progress")
def audio_progress(key: str):
    if (CACHE_DIR / f"{key}.m4a").exists():
        return {"key": key, "state": "done", "percent": 100}
    with JOBS_LOCK:
        job = JOBS.get(key)
    if not job:
        raise HTTPException(404, "Onbekende taak")
    return {"key": key, **job}


def range_response(f: Path, request: Request, media_type: str):
    size = f.stat().st_size
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(f, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    start = int(m.group(1)) if m and m.group(1) else 0
    end = int(m.group(2)) if m and m.group(2) else size - 1
    end = min(end, size - 1)
    length = end - start + 1

    def chunks():
        with open(f, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                data = fh.read(min(65536, left))
                if not data:
                    break
                left -= len(data)
                yield data

    return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    })


@app.get("/api/audio")
def audio(key: str, request: Request):
    f = CACHE_DIR / f"{key}.m4a"
    if not f.is_file():
        raise HTTPException(404, "Audio is nog niet uitgepakt")
    return range_response(f, request, "audio/mp4")

# ---------------------------------------------------------------- speler

class ReloadBody(BaseModel):
    session_id: str
    subtitle_index: Optional[int] = None
    position_ms: int = 0


@app.post("/api/player/reload")
async def player_reload(body: ReloadBody):
    """Spoor uit, spoor aan, 1s terugspoelen - duwt de speler tot herladen."""
    idx = body.subtitle_index
    try:
        await jf_post(f"/Sessions/{body.session_id}/Command",
                      body={"Name": "SetSubtitleStreamIndex",
                            "Arguments": {"Index": "-1"}})
        if idx is not None and idx >= 0:
            await jf_post(f"/Sessions/{body.session_id}/Command",
                          body={"Name": "SetSubtitleStreamIndex",
                                "Arguments": {"Index": str(idx)}})
        target = max(0, body.position_ms - 1000)
        await jf_post(f"/Sessions/{body.session_id}/Playing/Seek",
                      params={"seekPositionTicks": target * 10_000})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Jellyfin weigerde het commando: {e}")
    return {"ok": True}


class SeekBody(BaseModel):
    session_id: str
    position_ms: int


@app.post("/api/player/seek")
async def player_seek(body: SeekBody):
    await jf_post(f"/Sessions/{body.session_id}/Playing/Seek",
                  params={"seekPositionTicks": max(0, body.position_ms) * 10_000})
    return {"ok": True}
