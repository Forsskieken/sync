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

# The group first, with the gid the share already uses.
getent group family || groupadd -g 1005 family

# Then the account. Second line instead of the first if it already exists.
id jan || useradd -u 1001 -g family -m -s /bin/bash jan
usermod -aG family jan

id jan                       # check: uid=1001 gid=1005(family)
install -d -o jan -g family /opt/subtitle-sync
```

`groupadd -g` and `useradd -u` are where the numbers come from. Pick the gid
that the media share already uses — `ls -n` on it shows the number — rather
than letting the system choose one.

**As that user:**

### Getting the files there

Two ways. The directory must be empty either way.

Do this as the service account, not as root — files cloned by root are owned by
root and the service cannot build its virtualenv. Cloned as root anyway?
`chown -R jan:family /opt/subtitle-sync` puts it right.

**A — clone, if the machine has a key GitHub knows.** Check with
`ssh -T git@github.com`; it should answer with the repository name. It has none?
Make one and register it as a **read-only deploy key** on this repository
(Settings → Deploy keys), which is scoped to this repo alone:

```bash
ssh-keygen -t ed25519 -C "$(hostname) subtitle-sync" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub        # paste this into the deploy key
```

**B — copy from a machine that already has the files.** No key needed on the
target. Run this on the machine that has the checkout:

```bash
rsync -a --exclude .venv --exclude .git \
      /path/to/subtitle-sync/ jan@<host>:/opt/subtitle-sync/
```

`--exclude .venv` matters: a virtualenv carries absolute paths in
`pyvenv.cfg` and in every shebang under `bin/`, so a copied one does not run.
Build it on the target instead, as the next step does.

**As that user:**

```bash
su - jan
cd /opt/subtitle-sync
# Mind the trailing dot: without it git makes a sync/ subdirectory and every
# step below then fails on a file that is one level down.
git clone git@github.com:Forsskieken/sync.git .    # route A only

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp subtitle-sync.env.example subtitle-sync.env && chmod 600 subtitle-sync.env
nano subtitle-sync.env             # Jellyfin URL and API key
exit
```

**As root again, to start it:**

```bash
cd /opt/subtitle-sync
cp subtitle-sync.service.example       /etc/systemd/system/subtitle-sync.service
cp subtitle-sync-clean.service.example /etc/systemd/system/subtitle-sync-clean.service
cp subtitle-sync-clean.timer.example   /etc/systemd/system/subtitle-sync-clean.timer
nano /etc/systemd/system/subtitle-sync.service    # User, Group, ReadWritePaths
systemctl daemon-reload
systemctl enable --now subtitle-sync subtitle-sync-clean.timer
systemctl status subtitle-sync --no-pager
journalctl -u subtitle-sync -f
```

The timer empties the audio cache nightly; the service rebuilds what it needs.
Keep `CACHE_DIR` out of `/tmp`: `PrivateTmp=yes` gives the service a `/tmp` of
its own, so a cleaner running outside it would empty the wrong directory and the
real cache would grow unseen. `CacheDirectory=subtitle-sync` puts it in
`/var/cache/subtitle-sync` instead, owned by the service account.

`ReadWritePaths=` in the unit must list the same directories as `ALLOWED_ROOTS`
in the env file. Everything else is read-only, so a mismatch shows up as
"Read-only file system" when saving.

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
| `CACHE_DIR` | Where converted audio is kept. `CacheDirectory=` in the unit creates it |

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
