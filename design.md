# RipWatch — DVD Rip Watcher & Jellyfin Ingest Script

**Design Document · v1.2 · 2026-08-09** — adds interlace auto-detection (idet → yadif); source cleanup (delete MKV after transcode, local MP4 after copy); config.yaml file ledger replaces `.state.json`

A folder-watching pipeline that picks up MKV files ripped from DVDs, transcodes them to Jellyfin-friendly MP4 with ffmpeg, renames them to match Jellyfin's naming conventions, and copies the result onto the SMB share where the media server library lives.

---

## 1. Overview

```
 ┌─────────────┐      ┌──────────────┐      ┌──────────────┐      ┌─────────────┐
 │  Rip tool    │ mkv  │   Watcher    │      │  Transcoder  │      │  Uploader   │
 │ (MakeMKV /  ─┼────▶ │  (poll dir,  │────▶ │  (ffmpeg →   │────▶ │ (copy to    │
 │  disc drive) │      │  stability)  │      │  named .mp4) │      │  SMB share) │
 └─────────────┘      └──────────────┘      └──────────────┘      └─────────────┘
                        config.yaml           Jellyfin naming        atomic rename,
                        + .state.json         applied here           overwrite OK
```

Two files make up the tool:

| File          | Role |
|---------------|------|
| `ripwatch.sh` | Bash entry point. Verifies prerequisites (`ffmpeg`, `ffprobe`, `python3`), then `exec`s the Python script, forwarding all flags. |
| `ripwatch.py` | The actual daemon. Standard library only — no pip installs. |

Invocation:

```bash
./ripwatch.sh --input ~/rips/firefly-s1 --output "/Volumes/media/TV Shows"
```

The script runs forever (Requirement 6) until you press `Ctrl-C` (or `q` if we add key handling later). One watched directory = one show-season or one movie, defined by the `config.yaml` sitting in that directory.

---

## 2. The Bash wrapper — `ripwatch.sh`

Kept deliberately thin. Its only jobs are environment validation (Requirement 2) and launching Python:

```bash
#!/usr/bin/env bash
set -euo pipefail

for bin in ffmpeg ffprobe python3; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "error: '$bin' not found on PATH." >&2
    [[ "$bin" == python3 ]] || echo "  install with: brew install ffmpeg" >&2
    exit 1
  fi
done

exec python3 "$(dirname "$0")/ripwatch.py" "$@"
```

`ffprobe` ships with ffmpeg and is required because the transcoder inspects streams before building the ffmpeg command (see §6). `exec` replaces the shell process, so `Ctrl-C` and exit codes flow straight through to Python.

---

## 3. CLI

| Flag | Required | Default | Purpose |
|------|----------|---------|---------|
| `--input DIR` | yes | — | Directory to watch for incoming `.mkv` files (Requirement 3). Must contain `config.yaml`. |
| `--output DIR` | yes | — | Library root on the SMB mount, e.g. `/Volumes/media/TV Shows` (Requirement 4). The script builds the Jellyfin folder structure beneath it. |
| `--stable-seconds N` | no | `10` | How long a file's size must be unchanged before processing (Requirement 8). |
| `--poll-interval N` | no | `2` | Seconds between directory scans. |
| `--encoder NAME` | no | `libx264` | H.264 encoder. Escape hatch for `h264_videotoolbox` if a rip session needs speed over quality. |
| `--deinterlace MODE` | no | `auto` | `auto` = detect interlacing per file with ffmpeg's `idet` filter and apply `yadif` only when needed (§6); `on` = always deinterlace; `off` = never. |
| `--keep-source` | no | off | Disable cleanup: keep source MKVs and local MP4s instead of deleting them as each stage succeeds (§5). Useful for a trial run on a new setup. |

Parsed with stdlib `argparse`. Both directories are validated at startup: input must exist and contain a readable `config.yaml`; output must exist and be writable (a fast `touch`-and-delete probe catches an unmounted SMB share before any work starts).

---

## 4. `config.yaml`

One per watched directory. Because the standard library has no YAML parser (Requirement 5 rules out PyYAML), the script includes a ~30-line parser for the **flat subset** of YAML we actually need: `key: value` lines, `#` comments, quoted or bare strings, integers. That subset is documented in the file header so it never surprises you.

**TV show:**

```yaml
# config.yaml — Firefly, season 1
type: show            # optional; inferred as "show" when `season` is present
name: Firefly
year: 2002            # optional but recommended (helps Jellyfin matching)
tmdb_id: 1437
season: 1
next_episode: 1       # optional, default 1 — the episode number for the NEXT file
```

**Movie:**

```yaml
type: movie           # optional; inferred as "movie" when `season` is absent
name: The Fifth Element
year: 1997
tmdb_id: 18
```

Config is loaded once at startup and reloaded whenever its mtime changes, so you can bump `season:` between discs without restarting the watcher.

**config.yaml is also the ledger.** Below your keys, the script maintains a managed section recording every file's lifecycle — this replaces a separate state file, so one glance at config.yaml shows both the settings and the history:

```yaml
# Firefly season 1 — my notes stay intact
name: Firefly
year: 2002
tmdb_id: 1437
season: 1

# --- ripwatch state (managed by ripwatch — do not edit below this line) ---
files:
  - file: "title_t00.mkv"
    status: "deleted"          # detected -> transcoding -> transcoded ->
    season: 1                  #   uploading -> uploaded -> deleted (or error)
    episode: 1
    output: "Firefly (2002) [tmdbid-1437]/Season 01/Firefly (2002) S01E01.mp4"
    size: 5714449
    detected_at: "2026-08-09 18:36:10"
    finished_at: "2026-08-09 18:36:19"
```

Everything **above** the marker is yours: the script preserves it verbatim, comments included, and re-reads it when you edit it. Everything **below** the marker is regenerated on every save — don't edit it. The episode counter is *derived* from the ledger (highest episode already assigned for the current season, +1, floored at `next_episode`), so restarts and season changes need no separate bookkeeping: change `season: 1` to `season: 2` between discs and numbering restarts at `next_episode` automatically because no ledger entries exist for season 2 yet.

---

## 5. Watcher & file state machine

The main loop scans `--input` every `--poll-interval` seconds for `*.mkv` files (case-insensitive; `config.yaml`, dotfiles, and the state file are ignored). Each file moves through a small state machine:

```
        size changed → reset timer
             ┌────┐
             ▼    │
  DETECTED/STABILIZING ─▶ QUEUED ─▶ TRANSCODING ─▶ TRANSCODED ─▶ UPLOADING ─▶ UPLOADED ─▶ DELETED
                                         │              ▲             │
                                         └─── ERROR ◀───┼─────────────┘
                                                        │
                                     (cleanup stage 1:  │  (cleanup stage 2: local MP4
                                      source MKV deleted│   + sidecars deleted after
                                      after transcode)──┘   successful copy → DELETED)
```

- **DETECTED → STABILIZING** — a new file is recorded with its current size and timestamp.
- **STABILIZING → QUEUED** — the size (checked via `os.stat` each poll) has not changed for `--stable-seconds` (10 s default). This is how we know MakeMKV/Finder has finished writing (Requirement 8). Files of size 0 never qualify.
- **QUEUED → TRANSCODING** — files are pulled off the queue **one at a time, in the order they became stable**. This ordering is what drives episode numbering (arrival order, per your choice): the first file to finish copying in becomes `next_episode`, the second becomes `next_episode + 1`, and so on. Single-flight transcoding also keeps the machine responsive while ffmpeg works.
- **TRANSCODING** — ffmpeg runs via `subprocess.Popen` with progress parsing (§6). The loop keeps polling for new files while ffmpeg runs; nothing blocks.
- **TRANSCODED — cleanup stage 1.** On ffmpeg success, sidecar subtitles (if any) are extracted from the MKV first — they have to come out *before* the source disappears — and then the **source MKV is deleted**. From this point the work-dir MP4 is the only copy, which is why `transcoded` is a persisted stage: a crash here resumes at upload, never at re-transcode (there's nothing left to re-transcode).
- **UPLOADING → UPLOADED** — the MP4 (and sidecars) are copied to the share (§7).
- **DELETED — cleanup stage 2.** After a verified copy, the **local MP4 and sidecars are deleted** from `work/`. This is the normal terminal state: the only remaining copy of the movie/episode is the one on the share.
- **ERROR** — transcode errors keep the source MKV (nothing was deleted yet); upload errors keep the local MP4 in `work/` and retry it automatically on the next start. `--keep-source` disables both cleanup stages, turning DELETED into a no-op after UPLOADED.

**Persistence — the config.yaml ledger** (§4) records each file's stage as it happens, so a `Ctrl-C` or crash resumes exactly where it left off: `detected/queued/transcoding` entries re-queue for transcode (source still present), `transcoded/uploading` entries with a surviving work-dir MP4 re-queue for upload only, and episode numbers are never reused because they're derived from the ledger itself. A `.ripwatch.lock` pid-file in the input directory prevents a second instance from watching the same folder — two watchers would race the ledger and delete each other's sources mid-transcode.

Concurrency model: **single thread**. `Popen` is non-blocking, `stat` calls are cheap, and one poll loop that also pumps the ffmpeg progress pipe is far simpler to reason about than threads — and trivially safe for the shared state file.

---

## 6. Transcoding — mapping your Handbrake preset to ffmpeg

Before building the command, the script runs `ffprobe -print_format json -show_streams -show_chapters` on the source. That drives per-stream decisions below.

| Handbrake setting | ffmpeg equivalent | Notes |
|---|---|---|
| MP4 container | output `.mp4` + `-movflags +faststart` | faststart relocates the moov atom so streaming starts instantly. |
| 1080p | `-vf "scale=-2:'min(1080,ih)'"` | Caps at 1080p, **never upscales** — DVD sources (480/576p) pass through at native resolution, matching Handbrake preset behavior. `-2` keeps width even for H.264. |
| Video: H.264 | `-c:v libx264 -preset medium -crf 20 -profile:v high -pix_fmt yuv420p` | CRF 20 ≈ Handbrake RF 20; visually transparent for DVD sources. |
| Framerate: 30 FPS | `-r 30` | Applied as a peak/output rate; 24 fps film content stays under it, 29.97i DVD content lands at ~30. |
| Align A/V Start | `-avoid_negative_ts make_zero` | Shifts timestamps so audio/video start together at t=0 — the closest ffmpeg analog to Handbrake's option. |
| Passthru Common Metadata | `-map_metadata 0` | Copies title/language/etc. from the MKV. |
| Keep all audio | `-map 0:a` + per-stream codec (below) | Every audio track is mapped, not just the first. |
| Keep all subtitles | `-map 0:s?` + per-stream codec (below) | `?` = don't fail if a rip has no subs. |
| Keep chapters | `-map_chapters 0` | (ffmpeg default, made explicit.) |
| — | `-y` | Overwrite output without prompting (Requirement 9). |

**Interlace auto-detection → `yadif`.** DVD TV content is very often 480i; encoding it as-is bakes combing artifacts into the H.264 output. In `--deinterlace auto` (the default), each file gets a two-step check before its command is built. First the fast path: if ffprobe reports `field_order` of `tt`/`bb`/`tb`/`bt` on the video stream, the file is interlaced — done. If `field_order` is missing or `progressive` (rips frequently mislabel this), the script samples the file with ffmpeg's detector:

```bash
ffmpeg -hide_banner -an -sn -dn -i input.mkv -frames:v 500 -vf idet -f null - 2>&1
```

and parses the `Multi frame detection: TFF: x BFF: y Progressive: z` summary from stderr. If `(TFF + BFF) > z`, the file is treated as interlaced. When interlaced, `yadif` is prepended to the filter chain — `-vf "yadif,scale=-2:'min(1080,ih)'"` — producing one progressive frame per frame (`yadif=0`, so 29.97i → 29.97p, which stays under the 30 fps cap). Progressive film-sourced content skips the filter entirely, costing nothing. The detection result is shown in the status table so you can see which files triggered it.

**Per-stream audio policy** (built from the ffprobe result): `aac`, `ac3`, `eac3`, `mp3` → `copy` (all legal in MP4, all direct-play in Jellyfin — and DVD audio is almost always AC-3, so the common case is a lossless passthrough). Anything else (`dts`, `pcm_*`, `truehd`, `flac`) → transcode that stream to `aac` at 640k for >2 channels, 192k for stereo, preserving channel layout. Expressed as `-c:a:0 copy -c:a:1 aac ...` per stream index.

**Per-stream subtitle policy — the one real MP4 gotcha.** DVD subtitles are bitmap VOBSUB (`dvd_subtitle`), and ffmpeg *can* mux them into MP4 (`-c:s copy`, stored as MP4 subpicture tracks); Jellyfin extracts or burns them when a client can't render them. Text subs (`subrip`/`ass`) become `mov_text`. Anything unmuxable (rare for DVD sources — e.g. PGS from a Blu-ray rip dropped in by mistake) is **extracted to a sidecar file** next to the MP4 (`Name S01E01.und.sub/.idx` or `.srt`), which Jellyfin picks up by filename — so "keep all subtitles" holds in every case rather than failing the whole transcode.

**Assembled example** (DVD episode, AC-3 audio, one VOBSUB track):

```bash
ffmpeg -hide_banner -y -i "title_t03.mkv" \
  -map 0:v:0 -map 0:a -map 0:s? -map_metadata 0 -map_chapters 0 \
  -vf "scale=-2:'min(1080,ih)'" -r 30 \
  -c:v libx264 -preset medium -crf 20 -profile:v high -pix_fmt yuv420p \
  -c:a copy \
  -c:s copy \
  -avoid_negative_ts make_zero -movflags +faststart \
  -progress pipe:1 -nostats \
  "work/Firefly (2002) S01E01.mp4"
```

`-progress pipe:1` emits machine-readable `out_time=` lines the watcher parses to render a live percentage (against the duration ffprobe reported). Transcodes land in a `work/` subdirectory of the input folder so a half-written MP4 is never mistaken for a source file or uploaded.

---

## 7. Naming & upload

Names follow the Jellyfin docs exactly ([movies](https://jellyfin.org/docs/general/server/media/movies), [shows](https://jellyfin.org/docs/general/server/media/shows)). Reserved characters `< > : " / \ | ? *` are stripped from `name` before use; year and TMDB ID are embedded because both docs call them out as improving match reliability.

**Show** (season and episode zero-padded to 2; Jellyfin explicitly says *not* to abbreviate season folders to `S01`):

```
<output>/Firefly (2002) [tmdbid-1437]/Season 01/Firefly (2002) S01E01.mp4
```

**Movie:**

```
<output>/The Fifth Element (1997) [tmdbid-18]/The Fifth Element (1997) [tmdbid-18].mp4
```

Since we're offline-only, episode files carry no episode *title* — Jellyfin resolves titles itself from the TMDB ID in the series folder, which is exactly what that ID is for.

**Upload = copy-then-rename.** `shutil.copyfile` writes to `<final-name>.part` on the share, then `os.replace` renames it into place. The rename is atomic on the SMB mount, so Jellyfin's library scanner never sees a half-copied file, and `os.replace` silently overwrites an existing file (Requirement 9). Free-space on the share is checked (`os.statvfs`) before copying, and the copied file's size is verified against the source afterward. Directories (`Show (Year) [tmdbid-…]/Season NN/`) are created with `os.makedirs(exist_ok=True)`.

---

## 8. Console display

A single status table, redrawn in place with ANSI escapes each poll tick (falling back to plain appended lines when stdout isn't a TTY) — this is Requirement 7's imported/transcoded/uploaded list:

```
RipWatch — Firefly (2002), Season 1          watching: ~/rips/firefly-s1
output: /Volumes/media/TV Shows              encoder: libx264   stable: 10s

  FILE             STATUS         →  OUTPUT                           
  title_t00.mkv    ✔ uploaded     →  Firefly (2002) S01E01.mp4
  title_t01.mkv    ✔ uploaded     →  Firefly (2002) S01E02.mp4
  title_t02.mkv    ⚙ transcoding 62%  Firefly (2002) S01E03.mp4
  title_t03.mkv    ⏳ stabilizing (4s)
  title_t04.mkv    • queued          will be S01E05

  2 uploaded · 1 transcoding · 2 waiting          Ctrl-C to exit
```

---

## 9. Requirements traceability

| # | Requirement | Where |
|---|---|---|
| 1 | Runs as a Bash script | `ripwatch.sh` entry point (§2) |
| 2 | Check for ffmpeg first | wrapper checks `ffmpeg` + `ffprobe` (§2) |
| 3 | `--input` flag | §3 |
| 4 | `--output` flag | §3 |
| 5 | Stdlib Python only | argparse/subprocess/shutil/json/os; mini YAML parser instead of PyYAML (§4) |
| 6 | Loop until exited | poll loop, `Ctrl-C` handled via `KeyboardInterrupt` for a clean shutdown (§5) |
| 7 | Display imported/transcoded/uploaded list | status table (§8) |
| 8 | Auto-start after 10 s of stable size | STABILIZING state, `--stable-seconds` (§5) |
| 9 | Overwrite existing output files | ffmpeg `-y` + `os.replace` on upload (§6, §7) |

Workflow steps 1–5 from your description map to: you create the dir + config (§4), watcher detects & stabilizes (§5), ffmpeg transcodes mkv→mp4 (§6), rename to Jellyfin convention (§7), copy to SMB target (§7).

## 10. Decisions, edge cases & open questions

Decided in our discussion: episode numbers come from **arrival order** plus the config's season/`next_episode`; **no TMDB API calls** (the ID is only embedded in names for Jellyfin to use); **libx264** is the default encoder with `--encoder` as the escape hatch; **interlace auto-detection** (`idet` → `yadif`, §6) is included in v1.0 with a `--deinterlace` override.

Edge cases the implementation will handle: a file that shrinks or disappears mid-stabilization is reset/forgotten; an unmounted SMB share fails fast at startup and pauses uploads (with a visible warning) if it drops mid-run rather than erroring every file; duplicate filenames from a second disc session are disambiguated by the state file having already consumed them.

Resolved since v1.1: source disposal is now automatic — the MKV is deleted after a successful transcode and the local MP4 after a successful copy (§5), with `--keep-source` as the opt-out; and per-file history lives in the config.yaml ledger (§4) instead of a separate state file.

Open for v1.3: multi-episode discs where one title file contains two episodes (`S01E01-E02` naming).