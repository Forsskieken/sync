# sync

Nudge subtitle timings while the film plays, on the TV or in the browser, and
write the corrected file back. One HTML page.

> **Half a project.** This repository holds the front-end only. The page calls
> nine `/api/…` endpoints and no server here answers them. The contract below is
> what the page expects; the server still has to be written.

## What the page does

- **Two sources.** Follow a player on the TV, or open the audio track locally in
  the browser and scrub through it.
- **Nudge.** ±1s, ±½s, ±0.1s, from the current cue onward or over the whole file.
- **Undo**, then **Save** — the server keeps a backup and reports its name.
- **Reload on TV** after saving, so the player picks up the new file at the same
  position.
- Arrow keys work: ←/→ shift 0.1 s, ↑/↓ shift 1 s.

## The API it expects

| Endpoint | Method | Sends | Expects back |
|---|---|---|---|
| `/api/session` | GET | — | `session_id`, `item_id`, `name`, `device`, `position_ms`, `paused`, `subtitles[]`, `subtitle_index` |
| `/api/browse?path=` | GET | — | `path`, `parent`, `dirs[]`, `videos[]`, `subtitles[]` |
| `/api/subtitle?path=` | GET | — | `cues[]` of `{start, end, text}` in ms |
| `/api/subtitle/save` | POST | `{path, cues[]}` | `{backup}` — path of the backup it wrote |
| `/api/audio/prepare` | POST | `{path}` | `{key, state, codec, copy}` |
| `/api/audio/progress?key=` | GET | — | `{state, percent, error}` |
| `/api/audio?key=` | GET | — | the audio stream |
| `/api/player/seek` | POST | `{session_id, position_ms}` | — |
| `/api/player/reload` | POST | `{session_id, subtitle_index, position_ms}` | — |

Everything is same-origin and relative, so the server that answers these also
serves `sync.html`. No host, port or key is hard-coded anywhere in the page.

## Still to decide

- Which player the TV side drives, and how the server reaches it.
- Where `browse` is allowed to look, and how it is stopped from walking outside
  that directory.
- Whether the audio conversion runs per request or is cached by key.
