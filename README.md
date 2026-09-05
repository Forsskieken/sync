# subtitle-sync

Nudge subtitle timings while the film plays — following a Jellyfin session on
the TV, or scrubbing the audio track in the browser — and write the corrected
file back with a backup.

FastAPI backend, one HTML page, no database.

## What it does

- **Two sources.** Follow the Jellyfin session on the TV, or open the audio
  locally and scrub through it.
- **Nudge.** ±1 s, ±½ s, ±0.1 s, from the current cue onward or over the whole
  file. Arrow keys work: ←/→ 0.1 s, ↑/↓ 1 s.
- **Undo**, then **Save** — the old file is kept as a backup first.
- **Reload on TV** after saving, so the player picks up the new file at the same
  position.
- Audio that Chrome cannot play is re-encoded to AAC in the background, with a
  progress bar; anything already playable is copied straight through.

## Install

Runs as an ordinary user, not as root — the service may overwrite subtitle
files. **`jan` and `family` below are an example**: use whatever account you
have and whatever group owns the media share. Needs `ffmpeg` for `ffprobe` and
the AAC conversion.

**Why a group.** The films and subtitles all belong to one group — `family`,
gid 1005 say — and only its members may write there. So the account does not
own the files; it joins that group. Inside a container the *number* is what
counts: gid 1005 there must be gid 1005 on the host, or the share reads as
nobody and saving fails. That makes the account uid 1001, gid 1005.

**As root, once:**

```bash
apt update && apt install -y git python3-venv ffmpeg
install -d -o jan -g family /opt/subtitle-sync
```

**As that user:**

```bash
su - jan
git clone git@github.com:Forsskieken/sync.git /opt/subtitle-sync
cd /opt/subtitle-sync

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp subtitle-sync.env.example subtitle-sync.env && chmod 600 subtitle-sync.env
$EDITOR subtitle-sync.env          # Jellyfin URL and API key
exit
```

**As root again, to start it:**

```bash
cp /opt/subtitle-sync/subtitle-sync.service.example /etc/systemd/system/subtitle-sync.service
$EDITOR /etc/systemd/system/subtitle-sync.service   # User, Group, ReadWritePaths
systemctl daemon-reload && systemctl enable --now subtitle-sync
systemctl status subtitle-sync --no-pager
journalctl -u subtitle-sync -f
```

`ReadWritePaths=` in the unit must list the same directories as `ALLOWED_ROOTS`
in the env file. Everything else is read-only, so a mismatch shows up as
"Read-only file system" when saving.

No SSH key on that machine? Copy the files across instead of cloning, from a
machine that has the repo:

```bash
rsync -a --exclude .venv /path/to/subtitle-sync/ jan@<host>:/opt/subtitle-sync/
```

Then open `http://127.0.0.1:8099/`. It listens on the loopback only, so reach it
over an SSH tunnel: `ssh -L 8099:127.0.0.1:8099 jan@<host>`.

The user needs read and write access to everything under `ALLOWED_ROOTS`.

## Configuration

All of it comes from `subtitle-sync.env`, which is mode 600 and never committed.

| Variable | Meaning |
|---|---|
| `JELLYFIN_URL` | Base URL of the Jellyfin server |
| `JELLYFIN_API_KEY` | Jellyfin API key. The only secret |
| `PATH_MAP` | JSON: how Jellyfin's paths map onto this machine's |
| `ALLOWED_ROOTS` | JSON list. Browsing and saving happen only inside these |
| `CACHE_DIR` | Where converted audio is kept |

## Security

- **No login.** The unit binds to `127.0.0.1` for that reason — the service may
  overwrite subtitle files anywhere under `ALLOWED_ROOTS`. Reach it over an SSH
  tunnel, or put a proxy with a password in front. Do not bind it to `0.0.0.0`.
- **Not as root.** It runs as an ordinary user who has access to `ALLOWED_ROOTS`
  and nothing more.
- Every path is resolved before use and must sit inside `ALLOWED_ROOTS`, so
  `..` and symlinks cannot walk out (`safe_path` in `app.py`).
- Saving writes a backup first and reports its name.

## The API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/session` | GET | Current Jellyfin session: item, device, position, subtitle tracks |
| `/api/browse?path=` | GET | Directories, videos and subtitles under a path |
| `/api/video/info` | GET | Audio codec and stream details of a video |
| `/api/subtitle?path=` | GET | The cues as `{start, end, text}` in ms |
| `/api/subtitle/save` | POST | Writes the cues back, returns the backup path |
| `/api/audio/prepare` | POST | Starts the conversion, returns a job key |
| `/api/audio/progress?key=` | GET | State and percentage of that job |
| `/api/audio?key=` | GET | The audio stream |
| `/api/player/seek` | POST | Move the TV player to a position |
| `/api/player/reload` | POST | Reload the subtitle track on the TV |
