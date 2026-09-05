#!/usr/bin/env python3
"""
sync - ondertitels bijregelen vanuit de zetel.

Leest de actieve Jellyfin-sessie, laadt de bijhorende .srt van schijf,
schrijft aangepaste tijden terug, en laat de speler het spoor herladen.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- config

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin.example.net:8096").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

# Jellyfin ziet /mnt/jellyfin/Serie/..., deze host ziet /mnt/Serie/...
PATH_MAP = json.loads(os.environ.get("PATH_MAP", '{"/mnt/jellyfin": "/mnt"}'))

# Buiten deze mappen wordt niets gelezen of geschreven.
ALLOWED_ROOTS = [Path(p) for p in json.loads(os.environ.get("ALLOWED_ROOTS", '["/mnt"]'))]

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/subtitle-sync"))
HERE = Path(__file__).parent

# Chrome speelt deze audiocodecs af; al de rest moet naar AAC.
PLAYABLE = {"aac", "mp3", "opus", "vorbis", "flac"}

app = FastAPI(title="sync")

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


def parse_srt(text: str) -> List[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
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
    return cues


def write_srt(path: Path, cues: List[dict]) -> Path:
    """Schrijft de srt. Maakt eenmalig een .orig backup."""
    backup = path.with_name(path.name + ".orig")
    if not backup.exists():
        shutil.copy2(path, backup)
    blocks = [
        f"{i}\n{ms_to_tc(c['start'])} --> {ms_to_tc(c['end'])}\n{c['text']}\n"
        for i, c in enumerate(cues, 1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return backup

# ---------------------------------------------------------------- paden

def safe_path(p: str) -> Path:
    q = Path(p).resolve()
    for root in ALLOWED_ROOTS:
        try:
            q.relative_to(root.resolve())
            return q
        except ValueError:
            continue
    raise HTTPException(403, f"Pad ligt buiten de toegelaten mappen: {p}")


def map_path(jf_path: str) -> str:
    for src, dst in PATH_MAP.items():
        if jf_path.startswith(src):
            return dst + jf_path[len(src):]
    return jf_path


def find_srts(video: Path) -> List[dict]:
    if not video.parent.is_dir():
        return []
    out = []
    for f in sorted(video.parent.iterdir()):
        if f.suffix.lower() == ".srt" and f.name.startswith(video.stem):
            out.append({"path": str(f), "name": f.name})
    return out

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
    cues = parse_srt(read_text(f))
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
        elif f.suffix.lower() in (".mp4", ".mkv", ".avi", ".m4v", ".ts", ".webm"):
            vids.append({"name": f.name, "path": str(f), "subtitles": find_srts(f)})
    return {"path": str(d), "parent": str(d.parent), "dirs": dirs, "videos": vids}

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
