#!/usr/bin/env python3
"""
RipWatch — DVD rip watcher & Jellyfin ingest daemon.

Watches an input directory for .mkv files ripped from DVD, waits for each
file's size to be stable, transcodes it to Jellyfin-friendly MP4 with ffmpeg,
names it per Jellyfin conventions using the directory's config.yaml, and
copies the result to the output library (e.g. an SMB mount).

Cleanup policy (disable with --keep-source):
  * the source MKV is deleted after a successful transcode
  * the local MP4 (in work/) is deleted after a successful copy to the share

State lives in config.yaml itself: the script appends a managed section
below a marker line with a `files:` ledger recording each file's lifecycle
(detected -> transcoding -> transcoded -> uploading -> uploaded -> deleted).
Everything ABOVE the marker is yours and is preserved verbatim, comments
included. Edit your keys any time; never edit below the marker.

Standard library only. Launched via ripwatch.sh, which verifies that
ffmpeg/ffprobe exist.

config.yaml (flat "key: value" YAML subset — see parse_simple_yaml):

    # TV show                          # Movie
    name: Firefly                      name: The Fifth Element
    year: 2002                         year: 1997
    tmdb_id: 1437                      tmdb_id: 18
    season: 1
    next_episode: 1    # optional, default 1

`type: show|movie` may be given explicitly; otherwise it is inferred from
the presence of `season`.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "config.yaml"
WORK_DIRNAME = "work"
LOCK_FILENAME = ".ripwatch.lock"

MANAGED_MARKER = "# --- ripwatch state (managed by ripwatch — do not edit below this line) ---"

# Audio codecs that are MP4-legal and direct-play friendly -> stream copy.
AUDIO_COPY_CODECS = {"aac", "ac3", "eac3", "mp3"}

# Text subtitle codecs -> convert to mov_text for MP4.
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt"}

# Bitmap subtitle codecs that ffmpeg can mux into MP4 (as mp4s subpictures).
MP4_BITMAP_SUB_CODECS = {"dvd_subtitle"}

# Windows/SMB-reserved characters, per the Jellyfin docs.
RESERVED_CHARS = '<>:"/\\|?*'

STATUS_GLYPH = {
    "stabilizing": "⏳",
    "detected": "⏳",
    "queued": "•",
    "transcoding": "⚙",
    "transcoded": "⚙",
    "uploading": "↑",
    "uploaded": "✔",
    "deleted": "✔",
    "error": "✗",
}

# Lifecycle stages persisted to the config.yaml ledger.
LEDGER_FIELDS = ("status", "season", "episode", "output",
                 "size", "detected_at", "finished_at", "error")

COPY_CHUNK = 8 * 1024 * 1024  # 8 MiB chunks for upload progress


def now_stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log_line(msg):
    return "[{}] {}".format(time.strftime("%H:%M:%S"), msg)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{:.1f}{}".format(n, unit) if unit != "B" else "{}B".format(n)
        n /= 1024


# ---------------------------------------------------------------------------
# Minimal YAML (flat subset) parser + ledger parser/writer
# ---------------------------------------------------------------------------

def _scalar(value, lineno):
    value = value.strip()
    if value.startswith(("'", '"')) and len(value) >= 2:
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            raise ValueError("config.yaml line {}: unterminated quote".format(lineno))
        return value[1:end]
    value = value.split(" #", 1)[0].rstrip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def parse_simple_yaml(text):
    """Parse the flat `key: value` subset of YAML (the user section).

    Supported: `key: value` lines, blank lines, full-line and trailing
    `#` comments, single/double-quoted or bare scalar values, integers.
    Not supported (by design): nesting, lists, multi-line values.
    """
    result = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(
                "config.yaml line {}: expected 'key: value', got: {!r}".format(lineno, raw))
        key, _, value = line.partition(":")
        key = key.strip()
        if value.strip() == "":
            raise ValueError("config.yaml line {}: empty value for '{}'".format(lineno, key))
        result[key] = _scalar(value, lineno)
    return result


def parse_ledger(text):
    """Parse the managed section: a `files:` list of `- file: ...` entries."""
    files = {}
    current = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "files:":
            continue
        if stripped.startswith("- file:"):
            name = _scalar(stripped[len("- file:"):], lineno)
            current = {}
            files[str(name)] = current
        elif ":" in stripped and current is not None:
            key, _, value = stripped.partition(":")
            current[key.strip()] = _scalar(value, lineno)
    for rec in files.values():
        out = rec.pop("output", None)
        if out:
            rec["output_dir"], rec["output_name"] = os.path.split(str(out))
    return files


def _quote(value):
    return '"{}"'.format(str(value).replace('"', ""))


def dump_ledger(files):
    lines = [MANAGED_MARKER, "files:"]
    for name, rec in files.items():
        lines.append("  - file: {}".format(_quote(name)))
        for key in LEDGER_FIELDS:
            if key == "output":
                if rec.get("output_name"):
                    val = os.path.join(rec.get("output_dir", ""), rec["output_name"])
                    lines.append("    output: {}".format(_quote(val)))
                continue
            val = rec.get(key)
            if val is None:
                continue
            if isinstance(val, int):
                lines.append("    {}: {}".format(key, val))
            else:
                lines.append("    {}: {}".format(key, _quote(val)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    # Alternate spellings people plausibly type; all mean tmdb_id.
    TMDB_ALIASES = ("tmdb_id", "tmdbid", "tmdb", "tmdb-id")
    KNOWN_KEYS = {"name", "year", "season", "next_episode", "type"} | set(TMDB_ALIASES)

    def __init__(self, data):
        self.warnings = []
        self.name = str(data.get("name", "")).strip()
        if not self.name:
            raise ValueError("config.yaml: 'name' is required")
        self.year = data.get("year")
        self.tmdb_id = next(
            (data[k] for k in self.TMDB_ALIASES if data.get(k) is not None), None)
        if self.tmdb_id is None:
            self.warnings.append(
                "no tmdb_id in config.yaml — folders will be created WITHOUT "
                "the [tmdbid-...] tag and Jellyfin matching may be unreliable")
        self.season = data.get("season")
        self.next_episode = data.get("next_episode", 1)
        for key in data:
            if key not in self.KNOWN_KEYS:
                self.warnings.append(
                    "unknown key '{}' in config.yaml (typo?) — ignored".format(key))
        ctype = data.get("type")
        if ctype is None:
            ctype = "show" if self.season is not None else "movie"
        if ctype not in ("show", "movie"):
            raise ValueError("config.yaml: 'type' must be 'show' or 'movie'")
        if ctype == "show" and self.season is None:
            raise ValueError("config.yaml: type 'show' requires 'season'")
        self.type = ctype


class ConfigStore:
    """Owns config.yaml: user section (verbatim, above the marker) + managed
    ledger (below the marker, regenerated on every save)."""

    def __init__(self, input_dir):
        self.path = os.path.join(input_dir, CONFIG_FILENAME)
        self.user_text = ""
        self.files = {}          # filename -> record dict
        self.cfg = None
        self.known_mtime = 0.0

    def load(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if MANAGED_MARKER in text:
            user_text, _, managed = text.partition(MANAGED_MARKER)
        else:
            user_text, managed = text, ""
        self.user_text = user_text
        self.cfg = Config(parse_simple_yaml(user_text))
        self.files = parse_ledger(managed)
        self.known_mtime = os.stat(self.path).st_mtime

    def reload_user(self):
        """Re-read user keys after an external edit; the in-memory ledger
        stays authoritative (the script is the only legitimate writer)."""
        with open(self.path, "r", encoding="utf-8") as fh:
            text = fh.read()
        user_text = text.partition(MANAGED_MARKER)[0]
        self.cfg = Config(parse_simple_yaml(user_text))
        self.user_text = user_text
        self.known_mtime = os.stat(self.path).st_mtime

    def changed_on_disk(self):
        try:
            return os.stat(self.path).st_mtime != self.known_mtime
        except OSError:
            return False

    def save(self):
        text = self.user_text.rstrip("\n") + "\n\n" + dump_ledger(self.files)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)
        self.known_mtime = os.stat(self.path).st_mtime

    def next_episode(self):
        """Derived: continue after the highest episode already assigned for
        the current season; never below the config's next_episode."""
        season = self.cfg.season
        assigned = [rec.get("episode") or 0 for rec in self.files.values()
                    if rec.get("season") == season and rec.get("episode")]
        return max(self.cfg.next_episode, (max(assigned) + 1) if assigned else 0)


def sanitize(name):
    cleaned = "".join(c for c in str(name) if c not in RESERVED_CHARS)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned


def base_title(cfg, with_tag):
    base = sanitize(cfg.name)
    if cfg.year is not None:
        base += " ({})".format(cfg.year)
    if with_tag and cfg.tmdb_id is not None:
        base += " [tmdbid-{}]".format(cfg.tmdb_id)
    return base


def show_paths(cfg, episode):
    series_dir = base_title(cfg, with_tag=True)
    season_dir = "Season {:02d}".format(cfg.season)
    fname = "{} S{:02d}E{:02d}.mp4".format(base_title(cfg, with_tag=False),
                                           cfg.season, episode)
    return os.path.join(series_dir, season_dir), fname


def movie_paths(cfg, label=None):
    # Movies are stored as bare files directly in the library root (no folder).
    fname = (base_title(cfg, with_tag=True)
             + (" - {}".format(label) if label else "") + ".mp4")
    return "", fname


# ---------------------------------------------------------------------------
# ffprobe / interlace detection
# ---------------------------------------------------------------------------

def ffprobe(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format", "-show_chapters", path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("ffprobe failed: " + out.stderr.strip()[:400])
    return json.loads(out.stdout)


def probe_duration(probe):
    try:
        return float(probe.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        return 0.0


def detect_interlaced(path, probe):
    """(is_interlaced, reason). Fast path: container field_order.
    Slow path: sample ~500 frames through ffmpeg's idet filter."""
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            fo = s.get("field_order")
            if fo in ("tt", "bb", "tb", "bt"):
                return True, "field_order={}".format(fo)
            break
    cmd = ["ffmpeg", "-hide_banner", "-nostats",
           "-an", "-sn", "-dn", "-i", path,
           "-frames:v", "500", "-vf", "idet", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)"
                  r"\s*Progressive:\s*(\d+)", out.stderr)
    if not m:
        return False, "idet inconclusive"
    tff, bff, prog = (int(g) for g in m.groups())
    if tff + bff > prog:
        return True, "idet TFF={} BFF={} Prog={}".format(tff, bff, prog)
    return False, "idet progressive"


# ---------------------------------------------------------------------------
# ffmpeg command construction
# ---------------------------------------------------------------------------

def audio_args(probe):
    args = ["-map", "0:a?"]
    astreams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    for i, s in enumerate(astreams):
        codec = (s.get("codec_name") or "").lower()
        if codec in AUDIO_COPY_CODECS:
            args += ["-c:a:{}".format(i), "copy"]
        else:
            channels = int(s.get("channels") or 2)
            bitrate = "640k" if channels > 2 else "192k"
            args += ["-c:a:{}".format(i), "aac", "-b:a:{}".format(i), bitrate]
    return args


def subtitle_plan(probe):
    """(embed, sidecar): embed = [(sub_index, mp4_codec)];
    sidecar = [(sub_index, ext, codec)]."""
    embed, sidecar = [], []
    sstreams = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    for i, s in enumerate(sstreams):
        codec = (s.get("codec_name") or "").lower()
        if codec in TEXT_SUB_CODECS:
            embed.append((i, "mov_text"))
        elif codec in MP4_BITMAP_SUB_CODECS:
            embed.append((i, "copy"))
        elif codec in ("hdmv_pgs_subtitle", "pgssub"):
            sidecar.append((i, "sup", codec))
        else:
            sidecar.append((i, "srt", codec))
    return embed, sidecar


def sub_lang(probe, sub_index):
    sstreams = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    if sub_index < len(sstreams):
        return (sstreams[sub_index].get("tags") or {}).get("language", "und")
    return "und"


def video_encoder_args(encoder):
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if encoder == "h264_videotoolbox":
        return ["-c:v", "h264_videotoolbox", "-q:v", "60", "-pix_fmt", "yuv420p"]
    return ["-c:v", encoder, "-pix_fmt", "yuv420p"]


def build_ffmpeg_cmd(src, dst, probe, encoder, deinterlace, include_subs=True):
    vf = (["yadif"] if deinterlace else []) + ["scale=-2:'min(1080,ih)'"]
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", src,
           "-map", "0:v:0"]
    cmd += audio_args(probe)
    embed, sidecar = subtitle_plan(probe)
    if include_subs:
        for (in_idx, _codec) in embed:
            cmd += ["-map", "0:s:{}".format(in_idx)]
        for out_idx, (_in_idx, codec) in enumerate(embed):
            cmd += ["-c:s:{}".format(out_idx), codec]
    cmd += ["-map_metadata", "0", "-map_chapters", "0",
            "-vf", ",".join(vf), "-r", "30"]
    cmd += video_encoder_args(encoder)
    cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
            "-progress", "pipe:1", dst]
    return cmd, sidecar


def extract_sidecar(src, probe, sub_index, ext, dst_no_ext):
    """Extract one subtitle stream to <dst_no_ext>.<lang>.<ext>.
    Returns the path, or None on failure."""
    lang = sub_lang(probe, sub_index)
    out = "{}.{}.{}".format(dst_no_ext, lang, ext)
    codec = "copy" if ext == "sup" else "srt"
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", src,
           "-map", "0:s:{}".format(sub_index), "-c:s", codec, out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return out if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------

class RipWatch:
    def __init__(self, args):
        self.input_dir = os.path.abspath(os.path.expanduser(args.input))
        self.output_dir = os.path.abspath(os.path.expanduser(args.output))
        self.stable_seconds = args.stable_seconds
        self.poll_interval = args.poll_interval
        self.encoder = args.encoder
        self.deinterlace_mode = args.deinterlace
        self.keep_source = args.keep_source
        self.work_dir = os.path.join(self.input_dir, WORK_DIRNAME)

        self.store = None        # ConfigStore (config + ledger)
        self.order = []          # filenames in first-seen order (display)
        self.tracking = {}       # filename -> {"size": int, "changed_at": float}
        self.queue = []          # filenames ready for their next stage
        self.active = None       # in-flight transcode job, or None
        self.upload_progress = 0.0
        self.messages = []
        self.is_tty = sys.stdout.isatty()

    @property
    def cfg(self):
        return self.store.cfg

    @property
    def files(self):
        return self.store.files

    # -- setup --------------------------------------------------------------

    def preflight(self):
        if not os.path.isdir(self.input_dir):
            sys.exit("error: input directory does not exist: " + self.input_dir)
        if not os.path.exists(os.path.join(self.input_dir, CONFIG_FILENAME)):
            sys.exit("error: no {} in {}".format(CONFIG_FILENAME, self.input_dir))
        if not os.path.isdir(self.output_dir):
            sys.exit("error: output directory does not exist (SMB share mounted?): "
                     + self.output_dir)
        probe = os.path.join(self.output_dir, ".ripwatch-write-test")
        try:
            with open(probe, "w") as fh:
                fh.write("x")
            os.remove(probe)
        except OSError as exc:
            sys.exit("error: output directory is not writable: {} ({})".format(
                self.output_dir, exc))
        self.acquire_lock()
        os.makedirs(self.work_dir, exist_ok=True)
        self.store = ConfigStore(self.input_dir)
        try:
            self.store.load()
        except ValueError as exc:
            sys.exit("error: {}".format(exc))
        for warning in self.cfg.warnings:
            self.say("WARNING: " + warning)
        self.restore_from_ledger()

    def acquire_lock(self):
        """One watcher per input directory: two instances would race on the
        ledger and delete each other's sources mid-transcode."""
        self.lock_path = os.path.join(self.input_dir, LOCK_FILENAME)
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path) as fh:
                    other = int(fh.read().strip())
                os.kill(other, 0)  # raises if not running
                sys.exit("error: another ripwatch (pid {}) is already watching {}"
                         .format(other, self.input_dir))
            except (ValueError, ProcessLookupError, PermissionError):
                pass  # stale lock from a crashed run
        with open(self.lock_path, "w") as fh:
            fh.write(str(os.getpid()))

    def release_lock(self):
        try:
            os.remove(self.lock_path)
        except OSError:
            pass

    def reload_config(self):
        if not self.store.changed_on_disk():
            return
        try:
            self.store.reload_user()
        except (ValueError, OSError) as exc:
            self.say("config.yaml error (keeping previous): {}".format(exc))
            self.store.known_mtime = os.stat(self.store.path).st_mtime
            return
        self.say("config reloaded: {} {}".format(
            self.cfg.name,
            "season {}".format(self.cfg.season) if self.cfg.type == "show" else "(movie)"))
        for warning in self.cfg.warnings:
            self.say("WARNING: " + warning)

    def restore_from_ledger(self):
        """Resume anything that was mid-flight when we last exited."""
        transcode_q, upload_q = [], []
        for name, rec in self.files.items():
            if name not in self.order:
                self.order.append(name)
            status = rec.get("status")
            if status in ("detected", "queued", "transcoding"):
                if os.path.exists(os.path.join(self.input_dir, name)):
                    rec["status"] = "queued"
                    transcode_q.append(name)
                else:
                    rec["status"] = "error"
                    rec["error"] = "source missing on restart"
            elif status in ("transcoded", "uploading", "uploaded", "error"):
                work = self.work_path(rec)
                if work and os.path.exists(work):
                    rec["status"] = "transcoded"
                    rec["error"] = None
                    upload_q.append(name)
                elif status == "uploaded":
                    rec["status"] = "deleted"  # crashed between copy & cleanup
        transcode_q.sort(key=lambda n: self.files[n].get("episode") or 0)
        upload_q.sort(key=lambda n: self.files[n].get("episode") or 0)
        self.queue.extend(upload_q + transcode_q)  # uploads first: free disk
        if self.queue:
            self.say("restored {} unfinished file(s) from previous run".format(
                len(self.queue)))
        self.store.save()

    # -- helpers ------------------------------------------------------------

    def say(self, msg):
        line = log_line(msg)
        self.messages.append(line)
        self.messages = self.messages[-8:]
        if not self.is_tty:
            print(line, flush=True)

    def rec(self, name):
        return self.files.setdefault(name, {})

    def work_path(self, rec):
        if not rec.get("output_name"):
            return None
        return os.path.join(self.work_dir, rec["output_name"])

    def work_sidecars(self, rec):
        """Sidecar subtitle files in work/ belonging to this output."""
        work = self.work_path(rec)
        if not work:
            return []
        stem = os.path.splitext(work)[0]
        return [p for p in glob.glob(glob.escape(stem) + ".*")
                if not p.endswith((".mp4", ".log"))]

    # -- scanning & stability ----------------------------------------------

    def scan(self):
        try:
            entries = sorted(os.listdir(self.input_dir))
        except OSError as exc:
            self.say("cannot read input dir: {}".format(exc))
            return
        now = time.monotonic()
        present = set()
        for entry in entries:
            if not entry.lower().endswith(".mkv") or entry.startswith("."):
                continue
            path = os.path.join(self.input_dir, entry)
            if not os.path.isfile(path):
                continue
            present.add(entry)
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            rec = self.rec(entry)
            status = rec.get("status")
            if status in ("queued", "transcoding", "transcoded",
                          "uploading", "uploaded", "error"):
                continue  # in the pipeline or needs manual attention
            if status == "deleted" and entry not in self.tracking:
                # source was deleted after processing, yet a file with this
                # name is present again -> it's a fresh rip (MakeMKV reuses
                # names like title_t00.mkv across discs). Archive the old
                # entry rather than erasing it: its episode number must stay
                # in the ledger, or the counter would reassign it and the
                # new rip would OVERWRITE the earlier episode on the share.
                self.archive_entry(entry)
                rec = self.rec(entry)
                status = None
            track = self.tracking.get(entry)
            if track is None:
                self.tracking[entry] = {"size": size, "changed_at": now}
                if entry not in self.order:
                    self.order.append(entry)
                rec["status"] = "detected"
                rec["size"] = size
                rec["detected_at"] = now_stamp()
                self.say("detected {} ({})".format(entry, human_size(size)))
                self.store.save()
            elif size != track["size"]:
                track["size"] = size
                track["changed_at"] = now
                rec["size"] = size
            elif size > 0 and now - track["changed_at"] >= self.stable_seconds:
                self.promote(entry, size)

        for entry in list(self.tracking):
            if entry not in present:
                rec = self.files.get(entry, {})
                if rec.get("status") == "detected":
                    del self.files[entry]
                    if entry in self.order:
                        self.order.remove(entry)
                    self.store.save()
                    self.say("{} removed before processing".format(entry))
                del self.tracking[entry]

    def archive_entry(self, entry):
        """Move a completed ledger entry aside under '<name> (rip N)' so its
        history — and crucially its episode number — survives a re-rip that
        reuses the same source filename."""
        old = self.files.pop(entry)
        n = 1
        while "{} (rip {})".format(entry, n) in self.files:
            n += 1
        key = "{} (rip {})".format(entry, n)
        self.files[key] = old
        if entry in self.order:
            self.order.insert(self.order.index(entry), key)
        else:
            self.order.append(key)
        self.say("re-rip of {} detected; previous entry archived as '{}'".format(
            entry, key))

    def promote(self, entry, size):
        """detected -> queued: assign the output name now, in arrival order."""
        rec = self.rec(entry)
        if self.cfg.type == "show":
            episode = self.store.next_episode()
            rel_dir, fname = show_paths(self.cfg, episode)
            rec["season"] = self.cfg.season
            rec["episode"] = episode
        else:
            label = None
            _, canonical_name = movie_paths(self.cfg)
            for other, other_rec in self.files.items():
                if other != entry and other_rec.get("output_name") == canonical_name:
                    label = sanitize(os.path.splitext(entry)[0])
                    break
            rel_dir, fname = movie_paths(self.cfg, label)
        rec.update({"status": "queued", "size": size,
                    "output_dir": rel_dir, "output_name": fname, "error": None})
        self.store.save()
        del self.tracking[entry]
        self.queue.append(entry)
        self.say("queued {} -> {}".format(entry, fname))

    # -- pipeline: transcode ------------------------------------------------

    def start_next_job(self):
        entry = self.queue.pop(0)
        rec = self.rec(entry)
        if rec.get("status") == "transcoded":
            self.do_upload_stage(entry)  # resumed job: source already gone
            return
        src = os.path.join(self.input_dir, entry)
        if not os.path.exists(src):
            rec["status"] = "error"
            rec["error"] = "source file disappeared"
            self.store.save()
            return
        try:
            probe = ffprobe(src)
        except (RuntimeError, json.JSONDecodeError) as exc:
            rec["status"] = "error"
            rec["error"] = str(exc)
            self.store.save()
            self.say("ERROR probing {}: {}".format(entry, exc))
            return

        if self.deinterlace_mode == "on":
            deint, why = True, "forced on"
        elif self.deinterlace_mode == "off":
            deint, why = False, "forced off"
        else:
            deint, why = detect_interlaced(src, probe)
        rec["deinterlace"] = deint

        work_out = self.work_path(rec)
        cmd, sidecar = build_ffmpeg_cmd(src, work_out, probe, self.encoder, deint)
        logpath = os.path.join(self.work_dir, entry + ".ffmpeg.log")
        logfh = open(logpath, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logfh)
        os.set_blocking(proc.stdout.fileno(), False)
        rec["status"] = "transcoding"
        self.store.save()
        self.active = {
            "entry": entry, "proc": proc, "probe": probe, "src": src,
            "work_out": work_out, "sidecar": sidecar, "logfh": logfh,
            "logpath": logpath, "duration": probe_duration(probe),
            "progress": 0.0, "retried": False,
        }
        self.say("transcoding {} ({}, deinterlace: {} [{}])".format(
            entry, rec["output_name"], "yes" if deint else "no", why))

    def pump_active(self):
        job = self.active
        proc = job["proc"]
        try:
            chunk = proc.stdout.read()
        except (OSError, ValueError):
            chunk = None
        if chunk:
            for m in re.finditer(rb"out_time_us=(\d+)", chunk):
                if job["duration"] > 0:
                    secs = int(m.group(1)) / 1_000_000
                    job["progress"] = min(0.999, secs / job["duration"])
        code = proc.poll()
        if code is None:
            return
        job["logfh"].close()
        if code != 0:
            self.handle_transcode_failure(job, code)
        else:
            self.after_transcode(job)

    def handle_transcode_failure(self, job, code):
        entry = job["entry"]
        rec = self.rec(entry)
        embed, _ = subtitle_plan(job["probe"])
        if embed and not job["retried"]:
            # Most plausible failure: a subtitle stream the MP4 muxer rejected.
            # Retry once without embedded subs; they become sidecars instead.
            self.say("ffmpeg failed on {}; retrying without embedded subtitles".format(entry))
            extra = [(i, "srt" if c == "mov_text" else "sup", c) for i, c in embed]
            cmd, _ = build_ffmpeg_cmd(job["src"], job["work_out"], job["probe"],
                                      self.encoder, rec.get("deinterlace", False),
                                      include_subs=False)
            logfh = open(job["logpath"], "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logfh)
            os.set_blocking(proc.stdout.fileno(), False)
            job.update({"proc": proc, "logfh": logfh, "retried": True,
                        "progress": 0.0, "sidecar": job["sidecar"] + extra})
            return
        rec["status"] = "error"
        rec["error"] = "ffmpeg exited {} (see {})".format(code, job["logpath"])
        self.store.save()
        self.say("ERROR transcoding {}: ffmpeg exited {} — log: {}".format(
            entry, code, job["logpath"]))
        self.active = None

    def after_transcode(self, job):
        """Transcode succeeded: extract sidecars from the source, then delete
        the source MKV (cleanup stage 1), then move on to upload."""
        entry = job["entry"]
        rec = self.rec(entry)

        # Sidecars must come out of the MKV *before* it is deleted.
        stem = os.path.splitext(job["work_out"])[0]
        for (idx, ext, codec) in job["sidecar"]:
            out = extract_sidecar(job["src"], job["probe"], idx, ext, stem)
            if out:
                self.say("extracted sidecar subtitle: {}".format(os.path.basename(out)))
            else:
                self.say("warning: could not extract subtitle stream {} ({})".format(
                    idx, codec))

        if not self.keep_source:
            try:
                os.remove(job["src"])
                self.say("deleted source {}".format(entry))
            except OSError as exc:
                self.say("warning: could not delete source {}: {}".format(entry, exc))
        try:
            os.remove(job["logpath"])
        except OSError:
            pass

        rec["status"] = "transcoded"
        self.store.save()
        self.active = None
        self.do_upload_stage(entry)

    # -- pipeline: upload + cleanup -----------------------------------------

    def do_upload_stage(self, entry):
        rec = self.rec(entry)
        work_out = self.work_path(rec)
        if not work_out or not os.path.exists(work_out):
            rec["status"] = "error"
            rec["error"] = "transcoded file missing from work/"
            self.store.save()
            return
        rec["status"] = "uploading"
        self.store.save()
        self.upload_progress = 0.0
        self.render()
        try:
            dest_dir = os.path.join(self.output_dir, rec["output_dir"])
            os.makedirs(dest_dir, exist_ok=True)
            final = os.path.join(dest_dir, rec["output_name"])
            self.copy_with_progress(work_out, final)
            sidecars = self.work_sidecars(rec)
            for sc in sidecars:
                self.copy_with_progress(sc, os.path.join(dest_dir, os.path.basename(sc)))
        except OSError as exc:
            rec["status"] = "error"
            rec["error"] = "upload failed: {}".format(exc)
            self.store.save()
            self.say("ERROR uploading {}: {} (local MP4 kept in work/)".format(entry, exc))
            return
        rec["status"] = "uploaded"
        rec["finished_at"] = now_stamp()
        self.store.save()
        self.say("uploaded {} -> {}".format(
            entry, os.path.join(rec["output_dir"], rec["output_name"])))

        if not self.keep_source:
            # Cleanup stage 2: the local MP4 (and its sidecars) after a
            # successful copy.
            failed = False
            for path in [work_out] + sidecars:
                try:
                    os.remove(path)
                except OSError as exc:
                    failed = True
                    self.say("warning: could not delete local {}: {}".format(
                        os.path.basename(path), exc))
            if not failed:
                rec["status"] = "deleted"
                self.say("deleted local transcode for {}".format(entry))
        self.store.save()

    def copy_with_progress(self, src, final):
        """Chunked copy to <final>.part, then atomic rename (overwrites)."""
        part = final + ".part"
        total = os.stat(src).st_size
        stat = os.statvfs(os.path.dirname(final))
        if stat.f_frsize * stat.f_bavail < total:
            raise OSError("not enough free space on share ({} needed)".format(
                human_size(total)))
        copied = 0
        with open(src, "rb") as src_fh, open(part, "wb") as dst_fh:
            while True:
                chunk = src_fh.read(COPY_CHUNK)
                if not chunk:
                    break
                dst_fh.write(chunk)
                copied += len(chunk)
                self.upload_progress = copied / total if total else 1.0
                self.render()
        if os.stat(part).st_size != total:
            os.remove(part)
            raise OSError("size mismatch after copy")
        os.replace(part, final)  # atomic; silently overwrites existing
        # paranoia before anything local gets deleted: confirm the final
        # file is really there at full size
        if not os.path.exists(final) or os.stat(final).st_size != total:
            raise OSError("destination verification failed: " + final)
        self.say("copied -> {}".format(final))

    # -- display ------------------------------------------------------------

    def render(self):
        if not self.is_tty:
            return
        cfg = self.cfg
        title = base_title(cfg, with_tag=False)
        head = "RipWatch — {}{}".format(
            title, ", Season {}".format(cfg.season) if cfg.type == "show" else " (movie)")
        lines = [
            head,
            "watching: {}   output: {}".format(self.input_dir, self.output_dir),
            "encoder: {}   deinterlace: {}   stable: {}s   keep-source: {}".format(
                self.encoder, self.deinterlace_mode, self.stable_seconds,
                "yes" if self.keep_source else "no"),
            "",
        ]
        counts = {"done": 0, "active": 0, "waiting": 0, "error": 0}
        now = time.monotonic()
        for name in self.order:
            rec = self.files.get(name, {})
            status = rec.get("status", "?")
            glyph = STATUS_GLYPH.get(status, " ")
            detail = ""
            if status == "detected":
                track = self.tracking.get(name)
                waited = int(now - track["changed_at"]) if track else 0
                detail = "stabilizing ({}s/{}s)".format(
                    min(waited, self.stable_seconds), self.stable_seconds)
                counts["waiting"] += 1
            elif status in ("queued", "transcoded"):
                detail = "{} -> {}".format(status, rec.get("output_name", "?"))
                counts["waiting"] += 1
            elif status == "transcoding":
                pct = int((self.active or {}).get("progress", 0) * 100) \
                    if self.active and self.active["entry"] == name else 0
                deint = " +yadif" if rec.get("deinterlace") else ""
                detail = "transcoding {}%{} -> {}".format(pct, deint,
                                                          rec.get("output_name", "?"))
                counts["active"] += 1
            elif status == "uploading":
                detail = "uploading {}% -> {}".format(
                    int(self.upload_progress * 100), rec.get("output_name", "?"))
                counts["active"] += 1
            elif status in ("uploaded", "deleted"):
                cleaned = ", cleaned up" if status == "deleted" else ""
                detail = "uploaded -> {}{}".format(rec.get("output_name", "?"), cleaned)
                counts["done"] += 1
            elif status == "error":
                detail = "ERROR: {}".format(rec.get("error", "unknown"))
                counts["error"] += 1
            lines.append("  {} {:<28} {}".format(glyph, name[:28], detail))
        if not self.order:
            lines.append("  (waiting for .mkv files...)")
        lines.append("")
        lines.append("  {} uploaded · {} active · {} waiting · {} error"
                     "        Ctrl-C to exit".format(counts["done"], counts["active"],
                                                     counts["waiting"], counts["error"]))
        if self.messages:
            lines.append("")
            lines.extend("  " + m for m in self.messages[-4:])
        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
        sys.stdout.flush()

    # -- main loop ----------------------------------------------------------

    def run(self):
        self.preflight()
        self.say("watching {} ({} mode)".format(self.input_dir, self.cfg.type))
        try:
            while True:
                self.reload_config()
                self.scan()
                if self.active:
                    self.pump_active()
                elif self.queue:
                    self.start_next_job()
                self.render()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        print()
        if self.active:
            entry = self.active["entry"]
            proc = self.active["proc"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                self.active["logfh"].close()
            except Exception:
                pass
            # partial transcode output is useless; the source is still there
            try:
                os.remove(self.active["work_out"])
            except OSError:
                pass
            rec = self.rec(entry)
            if rec.get("status") == "transcoding":
                rec["status"] = "queued"  # resume on next start, same episode
            self.store.save()
            print("interrupted {} — it will resume as {} on next start".format(
                entry, rec.get("output_name")))
        self.release_lock()
        print("state saved to config.yaml. bye.")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="ripwatch",
        description="Watch a directory for DVD rips (.mkv), transcode to "
                    "Jellyfin-ready MP4, and copy to a library share. "
                    "Sources are deleted as they clear each stage "
                    "(disable with --keep-source).")
    ap.add_argument("--input", required=True,
                    help="directory to watch (must contain config.yaml)")
    ap.add_argument("--output", required=True,
                    help="library root to copy finished files into (SMB mount)")
    ap.add_argument("--stable-seconds", type=int, default=10,
                    help="seconds a file's size must be unchanged before "
                         "processing (default: 10)")
    ap.add_argument("--poll-interval", type=float, default=2,
                    help="seconds between directory scans (default: 2)")
    ap.add_argument("--encoder", default="libx264",
                    choices=["libx264", "h264_videotoolbox"],
                    help="H.264 encoder (default: libx264)")
    ap.add_argument("--deinterlace", default="auto", choices=["auto", "on", "off"],
                    help="deinterlacing: auto-detect with idet, force on, or "
                         "force off (default: auto)")
    ap.add_argument("--keep-source", action="store_true",
                    help="never delete anything: keep source MKVs and local "
                         "MP4s (default: delete each after its stage succeeds)")
    RipWatch(ap.parse_args()).run()


if __name__ == "__main__":
    main()