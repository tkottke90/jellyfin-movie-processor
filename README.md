# RipWatch

> Important - All movies and TV shows I have ripped are for personal use only and collected from official copies of the media that I have purchased. I do not support piracy in any way and this tool is for personal use only. Please do not use this tool to rip media you do not own.

Watch a folder for DVD rips, transcode them for Jellyfin, name them correctly, and copy them to your media server — automatically.

RipWatch is a two-file tool (`ripwatch.sh` + `ripwatch.py`, standard-library Python only) that sits between your ripping software (MakeMKV, etc.) and your Jellyfin library. You rip discs into a watched folder; RipWatch does everything else:

1. **Detects** each new `.mkv` and waits until the file has finished copying (its size must hold still for 10 seconds)
2. **Transcodes** it to MP4 with ffmpeg — H.264, capped at 1080p and 30 fps, all audio tracks and subtitles kept, chapters preserved, interlaced DVD content automatically deinterlaced
3. **Names** it to match [Jellyfin's naming conventions](https://jellyfin.org/docs/general/server/media/shows) so your server matches metadata correctly, e.g. `Firefly (2002) [tmdbid-1437]/Season 01/Firefly (2002) S01E01.mp4`
4. **Uploads** it to your library share with an atomic copy (Jellyfin never sees a half-written file)
5. **Cleans up** — the source MKV is deleted after a successful transcode, and the local MP4 after a successful copy

A live status table in the terminal shows every file's progress, and a ledger inside `config.yaml` records the full history of what was detected, transcoded, uploaded, and deleted.

## Requirements

- **ffmpeg** (which includes ffprobe) — on macOS: `brew install ffmpeg`
- **Python 3.8+** — preinstalled on macOS; no pip packages needed
- Your media share mounted and writable (e.g. `/Volumes/media`)

## Quick start

```bash
# 1. One folder per show-season (or per movie), with a config.yaml in it
mkdir ~/rips/firefly-s1
cat > ~/rips/firefly-s1/config.yaml <<EOF
name: Firefly
year: 2002
tmdb_id: 1437
season: 1
EOF

# 2. Start watching
chmod +x ripwatch.sh          # first time only
./ripwatch.sh --input ~/rips/firefly-s1 --output "/Volumes/media/TV Shows"

# 3. Rip discs into ~/rips/firefly-s1 — episodes are numbered in the
#    order the files arrive. Ctrl-C to stop; restarting resumes cleanly.
```

For a movie, the config is just:

```yaml
name: The Fifth Element
year: 1997
tmdb_id: 18
```

The TMDB ID comes from the number in the show's or movie's URL on [themoviedb.org](https://www.themoviedb.org) (e.g. `themoviedb.org/tv/1437-firefly` → `1437`). It's embedded in the folder name so Jellyfin fetches the right metadata; RipWatch itself never calls any API.

## Command-line options

| Flag | Default | Purpose |
|------|---------|---------|
| `--input DIR` | *required* | Folder to watch. Must contain `config.yaml`. |
| `--output DIR` | *required* | Library root to copy finished files into (your SMB mount). |
| `--stable-seconds N` | `10` | How long a file's size must be unchanged before processing starts. |
| `--poll-interval N` | `2` | Seconds between folder scans. |
| `--encoder NAME` | `libx264` | `libx264` (best quality) or `h264_videotoolbox` (Apple hardware — much faster, slightly larger/softer output). |
| `--deinterlace MODE` | `auto` | `auto` detects interlaced content per file and deinterlaces only when needed; `on`/`off` force it. |
| `--keep-source` | off | Don't delete anything — keep source MKVs and local MP4s. Good for a trial run. |

## config.yaml reference

The config uses simple flat `key: value` lines (a small YAML subset — no nesting or lists in the section you write):

| Key | Required | Meaning |
|-----|----------|---------|
| `name` | yes | Show or movie title, used in folder and file names. |
| `tmdb_id` | recommended | TMDB identifier, embedded as `[tmdbid-…]` for reliable Jellyfin matching. |
| `year` | recommended | Release year, embedded as `(YYYY)`. |
| `season` | shows only | Season number. Its presence is what makes RipWatch treat the folder as a show. |
| `next_episode` | no (default 1) | Episode number given to the next new file. |
| `type` | no | `show` or `movie`; normally inferred from `season`. |

You can edit the config while RipWatch is running — it reloads automatically. The usual multi-disc flow is: rip disc 1's episodes, let them process, then rip disc 2; numbering continues automatically. When you finish a season, change `season: 1` to `season: 2` and numbering restarts at episode 1.

### The ledger

Below your keys, RipWatch maintains a marked, managed section — a `files:` list recording each file's journey:

```yaml
# --- ripwatch state (managed by ripwatch — do not edit below this line) ---
files:
  - file: "title_t00.mkv"
    status: "deleted"
    season: 1
    episode: 1
    output: "Firefly (2002) [tmdbid-1437]/Season 01/Firefly (2002) S01E01.mp4"
    size: 5714449
    detected_at: "2026-08-09 18:36:10"
    finished_at: "2026-08-09 18:36:19"
```

Everything above the marker is yours (comments preserved); everything below it is rewritten by the script. `status` walks through `detected → transcoding → transcoded → uploading → uploaded → deleted` (or `error`). `deleted` is the normal happy ending — it means both cleanup stages ran and the only remaining copy is the one on your share. This ledger is also how episode numbers survive restarts: the next episode is always one past the highest already assigned for the current season.

## What the transcode does

The ffmpeg settings mirror a Handbrake-style 1080p30 preset: MP4 container with faststart, H.264 (CRF 20, high profile), resolution capped at 1080p but **never upscaled** (DVDs stay at their native 480/576p), framerate capped at 30 fps, A/V start aligned, and common metadata passed through. All audio tracks are kept — MP4-compatible codecs (AC-3, AAC, E-AC-3, MP3) are copied bit-for-bit; anything else (DTS, PCM) is converted to AAC. All subtitles are kept: text subs are embedded as mov_text, DVD bitmap subs are muxed in directly, and anything the MP4 container refuses is automatically extracted to a sidecar file next to the video, which Jellyfin picks up by name. Chapters are preserved. Interlaced content (very common on TV-series DVDs) is detected per file and deinterlaced with yadif; film-sourced discs skip it.

## Behavior details

**Episode numbering is arrival order.** Files get episode numbers in the order they finish copying into the folder, so rip discs in broadcast order. If a rip goes wrong, note that the number was already consumed — set `next_episode` in the config to renumber going forward.

**Everything is resumable.** Ctrl-C (or a crash) at any point is safe: on restart, files that hadn't finished transcoding start over from the still-present MKV, and files that had finished transcoding (source already deleted) resume at the upload step. Episode numbers are never reused.

**Errors keep your data.** A transcode failure leaves the source MKV in place (nothing has been deleted at that point) and logs the ffmpeg output in `work/`. An upload failure keeps the local MP4 in `work/` and retries automatically on the next start. Errored files are shown in the status table with the reason.

**Overwrites are deliberate.** If an output file already exists on the share (say, you re-ripped episode 3), the new copy replaces it.

**Movies are bare files.** Unlike shows, movies are uploaded directly into the output root with no per-movie folder (e.g. `Movies/The Fifth Element (1997) [tmdbid-18].mp4`).

**Movies with multiple files.** A second, different MKV dropped into a movie folder becomes a Jellyfin "additional version" (`Movie (1997) [tmdbid-18] - <ripname>.mp4`) instead of overwriting the first.

**One watcher per folder.** A `.ripwatch.lock` file prevents accidentally starting two instances on the same folder. Multiple RipWatch instances on *different* folders are fine — e.g. one per show while you rip several box sets.

**The `work/` folder.** Transcodes are staged in `<input>/work/` before upload. It's empty when everything has processed cleanly; anything left in it after an error is there so you don't lose work.

## Troubleshooting

- *"another ripwatch is already watching"* — a previous run is still alive, or crashed hard; if you're sure nothing is running, delete `.ripwatch.lock` from the input folder.
- *Output folder errors at startup* — the SMB share probably isn't mounted; mount it and rerun. RipWatch checks writability before doing any work.
- *A file sat at "stabilizing" forever* — its size kept changing (still ripping) or it's zero bytes. It processes 10 s after the rip completes.
- *Wrong episode number assigned* — files are numbered by arrival; fix the config's `next_episode`, re-drop the file (the old number stays consumed), and delete the misnamed file from the share.
- *Transcode failed* — check the `.ffmpeg.log` file in `work/`; the source MKV is still in the input folder for a retry (delete the errored entry's file and drop it in again).
