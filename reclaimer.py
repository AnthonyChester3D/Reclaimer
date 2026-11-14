#!/usr/bin/env python3
"""Reclaimer - find the files in a Plex library that don't belong and fix them with presets, a show-aware scanner and a browser GUI.

    python reclaimer.py            # starts the GUI at http://localhost:36597 and opens your browser
    python reclaimer.py --port N   # different port

Needs ffmpeg + ffprobe on PATH. Settings live in reclaimer.json next to this file.
"""
import argparse, base64, csv, html, json, os, re, shutil, socket, statistics, subprocess, sys, tempfile, threading, time, webbrowser
from urllib.parse import quote, unquote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if os.name == "nt":   # no console windows flashing up for every ffmpeg/ffprobe call when running from the tray
    class _QuietPopen(subprocess.Popen):
        def __init__(self, *a, **k):
            k.setdefault("creationflags", 0x08000000)   # CREATE_NO_WINDOW
            super().__init__(*a, **k)
    subprocess.Popen = _QuietPopen

HERE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ASSETS = Path(getattr(sys, "_MEIPASS", HERE))          # index.html lives here (inside the exe when frozen)
os.environ["PATH"] = str(HERE) + os.pathsep + os.environ.get("PATH", "")
PREVIEW_DIR = HERE / "previews"
CONFIG_PATH, CACHE_PATH = HERE / "reclaimer.json", HERE / "reclaimer_cache.json"
GENRES_PATH = HERE / "reclaimer_genres.json"
GUESS_PATH = HERE / "reclaimer_guesses.json"   # online/offline genre guesses for shows setup couldn't place
ARR_PATH, TRANSCODES_PATH = HERE / "reclaimer_arr.json", HERE / "reclaimer_transcodes.json"   # show/movie folder -> {"nfo": [...], "plex": [...]}
STATE_PATH, LOG_PATH = HERE / "reclaimer_state.json", HERE / "reclaimer_log.csv"
CACHE_VERSION = 3
TMP_PREFIX, MARK_TAG = ".reclaimer-tmp-", "RECLAIMER"
TIERS = [480, 576, 720, 1080, 1440, 2160]
CODEC_FACTOR = {"hevc": 1.0, "av1": 1.0, "h264": 0.65, "vp9": 0.8}   # size of HEVC vs source at equal quality
LEGACY = 0.45
AUDIO_GUESS_KBPS = {"truehd": 3000, "dts": 1509, "flac": 900, "mlp": 3000, "ac3": 448, "eac3": 640,
                    "aac": 160, "mp3": 192, "opus": 128, "vorbis": 160}
MKV_SUBS = {"subrip", "ass", "ssa", "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "webvtt", "text", "mov_text"}
STAT_TAGS = ["BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES", "_STATISTICS_WRITING_APP",
             "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS"]

def preset(max_h, caps, cq, audio, hdr_to_sdr=True):
    return {"max_height": max_h, "caps": caps, "cq": cq, "audio": audio, "hdr_to_sdr": hdr_to_sdr, "burn_subs": "none",
            "ten_bit": False, "denoise": "none"}

DEFAULT_PRESETS = {
    "Live Action TV": preset(1080, {"1080": 5, "720": 3, "480": 1.5}, 26, "stereo"),
    "Anime":          {**preset(1080, {"1080": 3.5, "720": 2, "480": 1.2}, 25, "stereo"), "burn_subs": "ass"},
    "Cartoons HD":    preset(1080, {"1080": 3, "720": 1.8, "480": 1}, 27, "stereo"),
    "Cartoons SD":    preset(480,  {"480": 1}, 27, "stereo"),
    "Movies HD":      preset(1080, {"1080": 8, "720": 4, "480": 2}, 24, "surround"),
    "Movies SD":      preset(480,  {"480": 1.5}, 25, "surround"),
    "Movies 4K":      preset(2160, {"2160": 16, "1080": 8, "720": 4, "480": 2}, 24, "surround", False),
    "Cinematic TV":   preset(1080, {"1080": 7, "720": 4, "480": 2}, 24, "stereo"),
    "Sitcom":         preset(1080, {"1080": 3, "720": 2, "480": 1.2}, 27, "stereo"),
    "True Crime":     preset(720,  {"720": 1.8, "480": 1}, 28, "stereo"),
    "CG Animation":   preset(1080, {"1080": 5, "720": 3, "480": 1.5}, 25, "surround"),
    "Talk & Reality": preset(720,  {"720": 1.8, "480": 1}, 28, "stereo"),
    "Documentary":    preset(1080, {"1080": 6, "720": 3.5, "480": 1.8}, 24, "stereo"),
    "Concerts":       preset(1080, {"1080": 8, "720": 4, "480": 2}, 24, "surround"),
    "Sports":         preset(1080, {"1080": 8, "720": 5, "480": 2.5}, 23, "surround"),
    "Classic Film":   {**preset(1080, {"1080": 10, "720": 5, "480": 2.5}, 23, "surround"), "denoise": "light"},
    "Dark & Moody":   {**preset(1080, {"1080": 9, "720": 4.5, "480": 2}, 23, "surround"), "ten_bit": True},
    "Lectures":       preset(720,  {"720": 1, "480": 0.6}, 30, "stereo"),
    "TV 4K":          preset(2160, {"2160": 12, "1080": 6, "720": 3.5, "480": 1.5}, 25, "surround", False),
}
# first matching rule wins; "A+B" = needs both genres; "A|B" = either one; "" = leave the library default
DEFAULT_RULES = [
    {"genre": "Anime", "tv": "Anime", "movies": "Anime"},
    {"genre": "Documentary+Crime|True Crime", "tv": "True Crime", "movies": "True Crime"},
    {"genre": "Reality|Reality TV|Reality-TV|Game Show|Talk|Talk Show|News|Stand-Up|Stand-Up Comedy", "tv": "Talk & Reality", "movies": "Talk & Reality"},
    {"genre": "Concert|Music+Documentary", "tv": "Concerts", "movies": "Concerts"},
    {"genre": "Education|Educational|How-To|Lecture", "tv": "Lectures", "movies": "Lectures"},
    {"genre": "Animation", "tv": "Cartoons HD", "movies": "CG Animation"},
    {"genre": "Sport|Sports", "tv": "Sports", "movies": ""},
    {"genre": "Documentary", "tv": "Documentary", "movies": "Documentary"},
    {"genre": "Horror", "tv": "Dark & Moody", "movies": "Dark & Moody"},
    {"genre": "Children|Kids", "tv": "Sitcom", "movies": ""},
    {"genre": "Science Fiction|Sci-Fi & Fantasy|Fantasy|Action|Action & Adventure|War|War & Politics|Western", "tv": "Cinematic TV", "movies": ""},
    {"genre": "Comedy|Soap", "tv": "Sitcom", "movies": ""},
]
# Last: everyday genres. Without it a plain drama or action movie counted as "couldn't place" even though
# the library default was the right answer.
GENERAL_RULE = {"genre": "Drama|Crime|Thriller|Mystery|Suspense|Romance|Family|Adventure|Biography|History|Musical|Music|Sport|War|Western|"
                "Action|Science Fiction|Fantasy|Comedy|Martial Arts|Superhero|Disaster|Film-Noir|Mini-Series|Miniseries|TV Movie|Indie|"
                "Short|Food|Travel|Home and Garden|Soap|Series", "tv": "Live Action TV", "movies": "Movies HD"}
DEFAULT_RULES.append(GENERAL_RULE)
DEFAULT_CONFIG = {
    "libraries": [],          # {"path": r"\\mynas\video\Movies", "type": "movies"|"tv", "preset": "Movies HD"}
    "presets": DEFAULT_PRESETS,
    "overrides": {},          # show/movie folder -> preset name
    "encoder": "hevc_nvenc",  # hevc_nvenc | libx265
    "hwaccel_decode": "auto",
    "tonemap": "auto",        # auto | libplacebo | zscale
    "originals": "move",      # move | delete
    "originals_folder": ".reclaimer_originals",
    "min_savings_percent": 15,
    "show_rule": "season",    # season | show | off
    "scan_threads": 8,
    "burn_subs_lang": "eng",  # subs in this language get burned in (presets with burn_subs) when there's no audio in it
    "extensions": [".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".webm", ".flv"],
    "ignore_dirs": ["#recycle", "#snapshot", "@eaDir"],
    "plex_url": "",           # e.g. http://mynas:32400
    "plex_token": "",
    "jellyfin_url": "", "jellyfin_key": "",   # e.g. http://mynas:8096 + an API key (Dashboard -> API Keys)
    "emby_url": "", "emby_key": "",
    "kodi_url": "", "kodi_user": "", "kodi_password": "",   # e.g. http://livingroom:8080 (Kodi's web server must be on)
    "plex_rules": DEFAULT_RULES,
    "plex_refresh": True,     # ask the media servers to rescan their libraries when the queue finishes
    "window": {"enabled": False, "start": 1, "end": 8},   # only encode between these hours (start > end = overnight)
    "pause_when_streaming": True,                         # wait while someone is watching Plex
    "auto_mode": False,                                   # rescan, queue and encode on its own
    "auto_interval_min": 30,
    "notify": True,           # Windows notification when a batch finishes
    "removed_folder": ".reclaimer_removed",   # duplicates and junk are moved here (per library) until you delete them
    "sonarr_url": "", "sonarr_key": "", "radarr_url": "", "radarr_key": "",
    "arr_guard": True,        # never downscale below a Sonarr/Radarr cutoff (it would re-download the better copy)
    "arr_rescan": True,       # ask Sonarr/Radarr to rescan what Reclaimer changed
    "verify": "full",         # full = decode the whole output + compare pictures; quick = compare pictures; off
    "verify_min_ssim": 0.80,  # a sampled picture scoring below this against the original fails the encode
    "protected": [],          # show/movie folders or files that are never touched
    "port": 36597,            # where the page lives: http://localhost:<port> (restart to apply)
    "online_lookup": False,   # look up genres on TVmaze/TMDB for things setup couldn't place (sends their names)
    "tmdb_key": "",           # optional free TMDB key: better matches, and movies too
    "setup_done": False,      # the setup questions have been answered
    "setup_presets": {},
    "setup_answers": {},      # last answers, so re-running setup starts from them      # what setup last produced, to tell which presets were edited by hand since
    "audio_lang_filter": False,   # drop audio tracks whose language isn't in audio_keep_langs
    "audio_keep_langs": ["eng"],
    "audio_keep_unknown": True,   # keep tracks with no language tag
    "audio_keep_default": True,   # keep the track flagged default (usually the original language)
}
IGNORE_MARKERS = (".reclaimerignore",)   # drop this file in a folder to hide it and everything below
PREVIEW = {"running": False, "msg": "", "result": None, "step": "", "progress": 0.0}
TRAY = {}
MEDIA = {"running": False, "msg": ""}
PRESET_DESCRIPTIONS = {
    "Live Action TV": "Everyday TV dramas and shows.",
    "Anime": "Japanese animation. Styled subtitles are burned into the picture so every TV and phone can show them.",
    "Cartoons HD": "Western cartoons in HD. Flat colours and clean lines shrink a lot without looking any different.",
    "Cartoons SD": "Older DVD-quality cartoons.",
    "Movies HD": "Most movies.",
    "Movies SD": "DVD-quality movies.",
    "Movies 4K": "Movies you want to keep in 4K and HDR. Trims oversized files without dropping them to 1080p.",
    "Cinematic TV": "Big-budget shows with effects and dark scenes. Gets a little more room than everyday TV.",
    "Sitcom": "Comedies and kids' shows. Simple sets and bright lighting shrink very well.",
    "True Crime": "True crime and interview-heavy documentaries.",
    "CG Animation": "Pixar-style 3D animation. Smooth gradients need a little more room than flat cartoons.",
    "Talk & Reality": "Reality TV, game shows, talk shows, news and stand-up.",
    "Documentary": "Nature and history documentaries. A little extra room so scenery stays sharp.",
    "Concerts": "Concert films. Surround sound is kept when you have a system for it.",
    "Sports": "Live sports. Constant fast motion, so it gets extra room.",
    "Classic Film": "Older films with visible film grain. Gently smooths the grain, which saves a lot of space.",
    "Dark & Moody": "Horror and other dark films. 10-bit colour keeps shadows smooth instead of blotchy.",
    "Lectures": "Courses, talks and tutorials. Slides and talking heads need very little.",
    "TV 4K": "Shows you want to keep in 4K and HDR.",
}
for _n, _p in DEFAULT_PRESETS.items():
    _p["desc"] = PRESET_DESCRIPTIONS.get(_n, "")
LOCK = threading.Lock()
CFG, CACHE, STATE, GENRES = {}, {}, {}, {}
ARR_MAP, TRANSCODES = {}, {}   # show/movie folder -> Sonarr/Radarr info; "file|device" -> Plex transcode record
ARR = {"running": False, "msg": ""}
JUNK = {"running": False, "items": [], "msg": ""}
VERIFY = {"running": False, "done": 0, "total": 0, "results": [], "msg": ""}
AUTO = {}        # memo: item folder -> (preset, source); cleared whenever genres, rules or the scan change
SCAN = {"running": False, "done": 0, "total": 0, "scope": "", "error": ""}
QUEUE = {"items": [], "running": False, "stop": False, "current": None, "saved": 0, "waiting": "", "force": False}
FILTERS = set()
_PROC = {"p": None, "cancel": False}
_PREV = {"p": None}                              # the preview's own ffmpeg, so it and the queue never stop each other


# ---------- small helpers ----------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default

def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def save_cache():
    save_json(CACHE_PATH, {"version": CACHE_VERSION, "files": CACHE})

def load_cache():
    d = load_json(CACHE_PATH, {})
    files = d.get("files", {}) if d.get("version") == CACHE_VERSION else {}
    for c in files.values():                     # apply title-based language guesses to files probed earlier too
        if c.get("info"):
            guess_languages(c["info"])
    return files

def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def norm(p):
    return os.path.normcase(os.path.normpath(p))

def inside(path, folder):
    """True if path is folder itself or lives under it (prefix match on whole path parts only)."""
    return norm(path) == norm(folder) or norm(path).startswith(norm(folder) + os.sep)

def tier_of(w, h):
    eff = max(h, w * 9 / 16) / 1.05
    return next((t for t in TIERS if t >= eff), TIERS[-1])

def fit(w, h, box_h):
    box_w = round(box_h * 16 / 9)
    if w <= box_w * 1.02 and h <= box_h * 1.02:
        return w, h
    r = min(box_w / w, box_h / h)
    return max(2, round(w * r / 2) * 2), max(2, round(h * r / 2) * 2)

def cap_for(caps, w, h):
    tiers = sorted((int(k), float(v)) for k, v in caps.items())
    eff = max(h, w * 9 / 16)
    for th, mbps in tiers:
        if eff <= th * 1.05:
            return mbps * 1e6
    return tiers[-1][1] * 1e6


# ---------- probing ----------
def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout else None

def stream_bps(s):
    b = num(s.get("bit_rate"))
    if not b:
        for k, v in (s.get("tags") or {}).items():
            if k.upper() in ("BPS", "BPS-ENG"):
                b = num(v)
    return b

def audio_bps(s):
    b = stream_bps(s)
    if b:
        return b
    if s.get("codec_name", "").startswith("pcm"):
        return num(s.get("sample_rate")) * int(s.get("channels") or 2) * int(s.get("bits_per_raw_sample") or 16)
    return AUDIO_GUESS_KBPS.get(s.get("codec_name"), 256) * 1000

def summarize(path, data):
    if not data:
        return None
    fmt, streams = data.get("format", {}), data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not vids:
        return None
    v = vids[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    dur = num(fmt.get("duration")) or num(v.get("duration"))
    size = int(num(fmt.get("size"))) or os.path.getsize(path)
    auds = [{"idx": a["index"], "codec": a.get("codec_name", "?"), "ch": int(a.get("channels") or 2), "bps": audio_bps(a),
             "lang": lang_code((a.get("tags") or {}).get("language")), "title": (a.get("tags") or {}).get("title", ""),
             "default": bool(a.get("disposition", {}).get("default"))} for a in audio]
    guess_languages({"audio": auds})
    vbps = stream_bps(v)
    if not vbps and dur:
        vbps = max(0.0, (num(fmt.get("bit_rate")) or size * 8 / dur) - sum(a["bps"] for a in auds))
    dv = next((sd for sd in v.get("side_data_list", []) if "DOVI" in sd.get("side_data_type", "").upper()), None)
    return {
        "path": str(path), "size": size, "duration": dur, "codec": v.get("codec_name", "?"),
        "width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "vbps": vbps,
        "field_order": v.get("field_order", ""),
        "fps": round(num((v.get("avg_frame_rate") or "0/1").split("/")[0]) / max(1.0, num((v.get("avg_frame_rate") or "0/1").split("/")[1] or 1)), 2),
        "color": {k: v.get(k) for k in ("color_primaries", "color_transfer", "color_space", "color_range")},
        "hdr": v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "dv_profile": int(dv.get("dv_profile") or 0) if dv else 0,
        "vidx": v["index"], "audio": auds,
        "subs": [{"idx": s["index"], "codec": s.get("codec_name", ""), "lang": (s.get("tags") or {}).get("language", "und"),
                  "title": (s.get("tags") or {}).get("title", ""), "forced": bool(s.get("disposition", {}).get("forced"))}
                 for s in streams if s.get("codec_type") == "subtitle"],
        "attachments": any(s.get("codec_type") == "attachment" for s in streams),
        "shrunk": MARK_TAG in {k.upper() for k in (fmt.get("tags") or {})},
    }

def find_videos(root):
    exts = {e.lower() for e in CFG["extensions"]}
    skip = {d.lower() for d in CFG["ignore_dirs"]} | {CFG["originals_folder"].lower()}
    for dirpath, dirs, files in os.walk(root):
        if any(m in files for m in IGNORE_MARKERS):
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d.lower() not in skip and not d.startswith((".", "@"))]
        for f in files:
            if not f.startswith(".") and os.path.splitext(f)[1].lower() in exts:
                yield os.path.join(dirpath, f)

def run_scan(scope, force):
    """scope: '' = all libraries, else a path prefix (a library or a show folder)."""
    try:
        roots = [l["path"] for l in CFG["libraries"]]
        if scope:
            roots = [scope] if any(inside(scope, r) for r in roots) else []
        paths = [p for r in roots if os.path.isdir(r) for p in find_videos(r)]
        todo = []
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            c = CACHE.get(p)
            if force or not c or c["size"] != st.st_size or c["mtime"] != st.st_mtime or (c.get("info") and "fps" not in c["info"]):
                todo.append((p, st))
        SCAN.update(total=len(todo), done=0)
        if not scope:  # full scan: forget files that no longer exist
            live = set(paths)
            for p in [p for p in CACHE if p not in live]:
                del CACHE[p]

        def work(item):
            p, st = item
            try:
                return p, st, summarize(p, ffprobe(p))
            except Exception:
                return p, st, None

        with ThreadPoolExecutor(CFG["scan_threads"]) as ex:
            for i, (p, st, info) in enumerate(ex.map(work, todo), 1):
                CACHE[p] = {"size": st.st_size, "mtime": st.st_mtime, "info": info}
                SCAN["done"] = i
                if i % 200 == 0:
                    save_cache()
        save_cache()
        read_nfos(scope)
        SCAN["genres"] = genre_report()
    except Exception as e:
        SCAN["error"] = str(e)
    finally:
        SCAN["running"] = False
        SCAN["phase"] = ""
        AUTO.clear()
    if not scope and arr_apps() and not ARR["running"]:
        ARR.update(running=True, msg="syncing…")
        arr_sync()
    if not scope and not JUNK["running"]:
        JUNK.update(running=True, msg="looking for junk…")
        find_junk()
    if not scope and servers() and not MEDIA["running"]:
        MEDIA.update(running=True, msg="syncing genres…")
        media_sync()


# ---------- planning ----------
def library_of(path):
    for lib in CFG["libraries"]:
        if inside(path, lib["path"]) and norm(path) != norm(lib["path"]):
            return lib
    return None

def item_key(path, lib):
    """Folder that identifies the movie or show (first folder under the library root)."""
    rel = Path(path).relative_to(lib["path"]).parts
    return str(Path(lib["path"]) / rel[0]) if len(rel) > 1 else path

def group_key(path, lib):
    rel = Path(path).relative_to(lib["path"]).parts
    if lib["type"] != "tv" or CFG["show_rule"] == "off" or len(rel) < 2:
        return None
    return str(Path(lib["path"], *rel[:2])) if CFG["show_rule"] == "season" and len(rel) > 2 else str(Path(lib["path"], rel[0]))

def item_genres(item):
    g = GENRES.get(item, {})
    return set().union(*(v for k, v in g.items() if k != "ids"))

def _auto(item, lib):
    if not AUTO:                                   # rebuild: one pass over the cache for every item's tallest video
        AUTO["_maxh"] = {}
        for info in all_infos():
            l = library_of(info["path"])
            if l:
                k = item_key(info["path"], l)
                AUTO["_maxh"][k] = max(AUTO["_maxh"].get(k, 0), info["height"])
    if item not in AUTO:
        g = GENRES.get(item, {})
        name = rule_preset(item_genres(item), lib["type"], item) if g else None
        src = "+".join(k for k in ("nfo", *SERVERS) if g.get(k)) if name else ""
        AUTO[item] = (name or lib.get("preset"), src)
    return AUTO[item]

def auto_preset(item, lib):
    """Preset before any manual override: genre rules (NFO files + Plex), else the library default."""
    return _auto(item, lib)[0]

def preset_for(item, lib):
    name = CFG["overrides"].get(item) or auto_preset(item, lib)
    return name, CFG["presets"].get(name) or next(iter(CFG["presets"].values()))

def typical(group_infos):
    """Most common resolution tier in the group, and the median video bitrate inside it."""
    if len(group_infos) < 3:
        return None
    tiers = [tier_of(i["width"], i["height"]) for i in group_infos]
    mode = min(sorted(set(tiers)), key=lambda t: (-tiers.count(t), t))
    med = statistics.median(i["vbps"] for i, t in zip(group_infos, tiers) if t == mode)
    return mode, med

LANG_ALIAS = {"fre": "fra", "ger": "deu", "chi": "zho", "dut": "nld", "cze": "ces", "gre": "ell", "per": "fas", "rum": "ron",
              "slo": "slk", "ice": "isl", "arm": "hye", "baq": "eus", "wel": "cym", "geo": "kat", "mac": "mkd", "may": "msa",
              "alb": "sqi", "bur": "mya", "tib": "bod", "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "ja": "jpn",
              "pt": "por", "ru": "rus", "zh": "zho", "ko": "kor", "nl": "nld", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
              "pl": "pol", "cs": "ces", "hu": "hun", "tr": "tur", "el": "ell", "he": "heb", "ar": "ara", "hi": "hin", "th": "tha"}
LANG_NAMES = {"eng": "English", "fra": "French", "deu": "German", "spa": "Spanish", "ita": "Italian", "jpn": "Japanese", "por": "Portuguese",
              "rus": "Russian", "zho": "Chinese", "kor": "Korean", "nld": "Dutch", "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
              "fin": "Finnish", "pol": "Polish", "ces": "Czech", "hun": "Hungarian", "tur": "Turkish", "ell": "Greek", "heb": "Hebrew",
              "ara": "Arabic", "hin": "Hindi", "tha": "Thai", "ukr": "Ukrainian", "vie": "Vietnamese", "ind": "Indonesian",
              "cat": "Catalan", "ron": "Romanian", "bul": "Bulgarian", "isl": "Icelandic", "tam": "Tamil", "tel": "Telugu",
              "hrv": "Croatian", "srp": "Serbian", "bos": "Bosnian", "slk": "Slovak", "slv": "Slovenian", "est": "Estonian",
              "lav": "Latvian", "lit": "Lithuanian", "msa": "Malay", "fil": "Filipino", "tgl": "Tagalog", "ben": "Bengali",
              "urd": "Urdu", "pan": "Punjabi", "mar": "Marathi", "guj": "Gujarati", "kan": "Kannada", "mal": "Malayalam",
              "fas": "Persian", "kat": "Georgian", "hye": "Armenian", "aze": "Azerbaijani", "kaz": "Kazakh", "uzb": "Uzbek",
              "mon": "Mongolian", "khm": "Khmer", "lao": "Lao", "mya": "Burmese", "sin": "Sinhala", "nep": "Nepali",
              "swa": "Swahili", "afr": "Afrikaans", "zul": "Zulu", "amh": "Amharic", "som": "Somali", "yor": "Yoruba",
              "cym": "Welsh", "gle": "Irish", "gla": "Scottish Gaelic", "eus": "Basque", "glg": "Galician", "ltz": "Luxembourgish",
              "mlt": "Maltese", "sqi": "Albanian", "mkd": "Macedonian", "bel": "Belarusian", "nob": "Norwegian Bokmål",
              "nno": "Norwegian Nynorsk", "yue": "Cantonese", "cmn": "Mandarin", "lat": "Latin", "epo": "Esperanto",
              "zxx": "No spoken language", "mul": "Multiple languages", "mis": "Uncoded language",
              "cpe": "English-based creole (likely mis-tagged)", "qaa": "Original language (unnamed)",
              "und": "Unknown / untagged"}
TITLE_LANGS = [(re.compile(p, re.I), code) for p, code in (
    (r"\b(english|eng)\b", "eng"), (r"\b(japanese|jpn|jap|nihongo)\b", "jpn"), (r"\b(spanish|español|espanol|castellano|latino|spa)\b", "spa"),
    (r"\b(french|français|francais|fre|fra|vf|vff)\b", "fra"), (r"\b(german|deutsch|ger|deu)\b", "deu"), (r"\b(italian|italiano|ita)\b", "ita"),
    (r"\b(portuguese|português|portugues|brazilian|por)\b", "por"), (r"\b(russian|русский|rus)\b", "rus"), (r"\b(korean|kor)\b", "kor"),
    (r"\b(mandarin|chinese|cantonese|chi|zho)\b", "zho"), (r"\b(hindi|hin)\b", "hin"), (r"\b(dutch|nederlands)\b", "nld"),
    (r"\b(polish|polski)\b", "pol"), (r"\b(swedish|svenska)\b", "swe"), (r"\b(danish|dansk)\b", "dan"),
    (r"\b(norwegian|norsk)\b", "nor"), (r"\b(finnish|suomi)\b", "fin"), (r"\b(turkish|türkçe)\b", "tur"),
    (r"\b(arabic)\b", "ara"), (r"\b(hungarian|magyar)\b", "hun"), (r"\b(czech|česky)\b", "ces"), (r"\b(ukrainian)\b", "ukr"),
    (r"\b(thai)\b", "tha"), (r"\b(greek)\b", "ell"), (r"\b(hebrew)\b", "heb"))]

def guess_languages(info):
    """Untagged audio tracks often say their language in the title ("English 5.1", "Japanese"): use it."""
    for a in info.get("audio", []):
        if a.get("lang") == "und" and a.get("title"):
            hits = {code for rx, code in TITLE_LANGS if rx.search(a["title"])}
            if len(hits) == 1:
                a["lang"], a["lang_guessed"] = hits.pop(), True
    return info

def lang_code(tag):
    t = (tag or "und").strip().lower()
    t = t.split("-")[0]                      # "en-US" -> "en"
    return LANG_ALIAS.get(t, t) or "und"

def keep_by_language(tracks):
    """Language filter from Settings. Returns the set of audio stream indexes to keep."""
    if not CFG["audio_lang_filter"] or len(tracks) < 2:
        return {a["idx"] for a in tracks}
    want = {lang_code(l) for l in CFG["audio_keep_langs"]}
    keep = {a["idx"] for a in tracks if a["lang"] in want
            or (a["lang"] == "und" and CFG["audio_keep_unknown"])
            or (a.get("default") and CFG["audio_keep_default"])}
    return keep or {a["idx"] for a in tracks}   # never leave a file silent

def language_summary():
    out = {}
    for info in all_infos():
        if not library_of(info["path"]):
            continue
        langs = {a["lang"] for a in info["audio"]}
        for a in info["audio"]:
            e = out.setdefault(a["lang"], {"lang": a["lang"], "name": LANG_NAMES.get(a["lang"], a["lang"]), "files": 0, "bytes": 0,
                                           "only": 0, "guessed": 0})
            e["bytes"] += a["bps"] * info["duration"] / 8
            e["guessed"] += bool(a.get("lang_guessed"))
        for l in langs:
            out[l]["files"] += 1
            if len(langs) == 1:
                out[l]["only"] += 1
    return sorted(out.values(), key=lambda e: -e["files"])

def language_items(lang):
    """Every show/movie with an audio track in this language, with what to double-check."""
    out = {}
    for info in all_infos():
        lib = library_of(info["path"])
        tracks = [a for a in info["audio"] if a["lang"] == lang]
        if not lib or not tracks:
            continue
        key = item_key(info["path"], lib)
        it = out.setdefault(key, {"title": Path(key).name, "path": key, "type": lib["type"], "files": [], "bytes": 0, "only": 0,
                                  "flags": 0, "genres": sorted(item_genres(key))[:4]})
        others = sorted({a["lang"] for a in info["audio"]} - {lang})
        tr = []
        for a in tracks:
            said = {c for rx, c in TITLE_LANGS if rx.search(a["title"] or "")} - {lang}
            tr.append({"idx": a["idx"], "title": a["title"], "codec": a["codec"], "ch": a["ch"], "guessed": bool(a.get("lang_guessed")),
                       "says": LANG_NAMES.get(next(iter(said)), next(iter(said))) if len(said) == 1 else "",
                       "says_code": next(iter(said)) if len(said) == 1 else ""})
            it["bytes"] += a["bps"] * info["duration"] / 8
        flag = any(t["says"] for t in tr)
        it["flags"] += flag
        it["only"] += not others
        it["files"].append({"name": Path(info["path"]).name, "path": info["path"], "others": [LANG_NAMES.get(o, o) for o in others],
                            "tracks": tr, "flag": flag, "says": next((t["says"] for t in tr if t["says"]), "")})
    for it in out.values():
        it["files"].sort(key=lambda f: f["name"].lower())
    return sorted(out.values(), key=lambda i: (-i["flags"], -i["bytes"]))

def audio_stream(path, idx):
    c = CACHE.get(path) or {}
    info = c.get("info")
    if not info or not library_of(path):
        raise ValueError("that file isn't in a scanned library")
    for n, a in enumerate(info["audio"]):
        if a["idx"] == idx:
            return info, n
    raise ValueError("that audio track wasn't found; rescan and try again")

def track_sample(path, idx):
    """10 seconds of one audio track, from about a third of the way in, so you can hear what language it is."""
    info, _ = audio_stream(path, idx)
    PREVIEW_DIR.mkdir(exist_ok=True)
    for old in PREVIEW_DIR.glob("sample-*.m4a"):
        old.unlink(missing_ok=True)
    out = PREVIEW_DIR / f"sample-{int(time.time() * 1000)}.m4a"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{info['duration'] / 3:.1f}", "-i", path, "-map", f"0:{idx}", "-t", "10",
                        "-vn", "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(out)], capture_output=True, text=True)
    if r.returncode or not out.exists():
        raise ValueError("couldn't read that track: " + (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
    return {"url": "/previews/" + out.name}

def relabel_track(path, idx, lang):
    """Fix a wrong language tag. Copies every stream as-is into a new file, checks it, then swaps it in."""
    lang = (lang or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", lang):
        raise ValueError("use a 3-letter language code, like eng or hin")
    if QUEUE["current"] == path:
        raise ValueError("that file is being encoded right now")
    info, n = audio_stream(path, idx)
    src = Path(path)
    tmp = src.with_name(f".reclaimer_relabel{src.suffix}")
    before = ffprobe(path) or {}
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", "0", "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
                        f"-metadata:s:a:{n}", f"language={lang}", str(tmp)], capture_output=True, text=True)
    try:
        after = ffprobe(tmp) if not r.returncode else None
        ok = after and len(after.get("streams", [])) == len(before.get("streams", [])) \
            and abs(num(after.get("format", {}).get("duration")) - num(before.get("format", {}).get("duration"))) < 1.5 \
            and lang_code(([s for s in after["streams"] if s.get("codec_type") == "audio"][n].get("tags") or {}).get("language")) == lang_code(lang)
        if not ok:
            raise ValueError("the relabelled copy didn't check out, so the original was left alone" +
                             (": " + r.stderr.strip().splitlines()[-1] if r.returncode and r.stderr.strip() else ""))
        st = src.stat()
        os.replace(tmp, src)
        os.utime(src, (st.st_atime, st.st_mtime))
    finally:
        if tmp.exists():
            tmp.unlink()
    nst, info = src.stat(), summarize(path, ffprobe(path))
    with LOCK:
        CACHE[path] = {"size": nst.st_size, "mtime": nst.st_mtime, "info": info}
        save_cache()
    print(f"Relabelled audio track {n + 1} of {src.name} as {LANG_NAMES.get(lang, lang)}")
    return {"ok": True, "name": LANG_NAMES.get(lang, lang)}

def plan_audio(info, mode):
    """Return (list of (idx, action, kbps_estimate), est_bps_total). action: copy|aac|eac3|drop."""
    def decide(a):
        c, ch, bps = a["codec"], a["ch"], a["bps"]
        if mode == "keep":
            return "copy", bps
        if mode == "stereo":
            return ("copy", bps) if c in ("aac", "mp3") and ch <= 2 and bps <= 260e3 else ("aac", 160e3)
        if c in ("aac", "ac3", "eac3") and ch <= 6 and bps <= 768e3:
            return "copy", bps
        return ("eac3", 448e3) if ch > 2 else ("aac", 160e3)

    kept = keep_by_language(info["audio"])
    decided = [(a, *decide(a)) if a["idx"] in kept else (a, "lang", 0) for a in info["audio"]]
    commentary = lambda a: "comment" in a["title"].lower()
    covered = {a["lang"] for a, act, _ in decided if act == "copy" and not commentary(a) and a["lang"] != "und"}
    out, est = [], 0.0
    for a, act, nb in decided:
        if act == "lang":
            act = "drop"
        elif act != "copy" and a["lang"] in covered and not commentary(a):
            act, nb = "drop", 0      # e.g. a TrueHD track next to an AC3 track of the same language
        out.append((a["idx"], act, nb))
        est += nb
    return out, est

IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
TEXT_SUBS = {"subrip", "webvtt", "mov_text", "text"}
SUB_NAMES = {"hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub", "dvb_subtitle": "dvb", "subrip": "srt", "mov_text": "srt"}

ASS_SUBS = {"ass", "ssa"}
BURN_MODES = {"none": set(), "ass": ASS_SUBS, "ass+image": ASS_SUBS | IMAGE_SUBS, True: ASS_SUBS, False: set()}

def burn_candidate(info, mode):
    """(stream index, 'ass'|'image') of the subtitle to burn in, or None. Only when the file has no audio in
    that language (so you'd need the subs to follow it), no plain SRT-type subs in it (those play everywhere),
    and a full, non-forced track of a burnable kind exists. ASS is preferred over image subs."""
    kinds = BURN_MODES.get(mode, set())
    lang = CFG["burn_subs_lang"][:2].lower()
    same = lambda s: s["lang"][:2].lower() == lang
    if not kinds or any(same(a) for a in info["audio"]) or any(same(s) and s["codec"] in TEXT_SUBS for s in info["subs"]):
        return None
    full = [s for s in info["subs"] if same(s) and s["codec"] in kinds and not s["forced"]
            and not any(w in s["title"].lower() for w in ("sign", "song", "forced"))]
    full.sort(key=lambda s: s["codec"] not in ASS_SUBS)
    return (full[0]["idx"], "ass" if full[0]["codec"] in ASS_SUBS else "image") if full else None

_MARKERS = {}                                   # folder -> (checked at, has marker); saves thousands of NAS lookups per page load

def has_marker(d):
    k, now = str(d), time.time()
    hit = _MARKERS.get(k)
    if not hit or now - hit[0] > 120:
        hit = _MARKERS[k] = (now, any((d / m).exists() for m in IGNORE_MARKERS))
    return hit[1]

def is_protected(path):
    if any(inside(path, k) for k in CFG["protected"]):
        return True
    d = Path(path).parent                       # marker file anywhere above the file, up to its library root
    lib = library_of(path)
    while lib and inside(str(d), lib["path"]):
        if has_marker(d):
            return True
        if norm(str(d)) == norm(lib["path"]):
            break
        d = d.parent
    return False

def evaluate(info, pname, p, typ):
    plan = {"preset": pname, "action": "ok", "reasons": [], "out_w": info["width"], "out_h": info["height"],
            "cap": 0, "audio": [], "hdr_to_sdr": False, "est_size": info["size"]}
    if is_protected(info["path"]):
        return {**plan, "action": "skip", "reasons": ["protected"]}
    if info["shrunk"]:
        plan["reasons"] = ["already reclaimed"]
        return plan
    prev = STATE.get(info["path"])
    if prev and prev.get("size") == info["size"]:
        return {**plan, "action": "skip", "reasons": [prev["reason"]]}
    tone = p["hdr_to_sdr"] and info["hdr"]
    if info["dv_profile"] == 5 and tone:
        return {**plan, "action": "skip", "reasons": ["Dolby Vision profile 5 can't be tone-mapped"]}
    ow, oh = fit(info["width"], info["height"], p["max_height"])
    cap = cap_for(p["caps"], ow, oh)
    if typ and tier_of(ow, oh) > typ[0]:                   # only ever lowers the preset's target
        ow, oh = fit(info["width"], info["height"], typ[0])
        cap = min(cap, max(typ[1], 0.3e6))
        plan["reasons"].append(f"most episodes are {typ[0]}p")
    guard = arr_floor(info["path"])
    if guard and tier_of(ow, oh) < guard[0] <= tier_of(info["width"], info["height"]):
        ow, oh = fit(info["width"], info["height"], guard[0])     # going lower would make Sonarr/Radarr re-download it
        top = max(int(k) for k in p["caps"])
        cap = cap_for(p["caps"], ow, oh) if guard[0] <= top else float(p["caps"][str(top)]) * 1e6 * (guard[0] / top) ** 2
        plan["reasons"].append(f"kept at {guard[0]}p for {guard[1]}")
        plan["arr_kept"] = True
    if info.get("fps", 0) > 40:
        cap *= 1.5                                    # caps assume ~24-30 fps; 50/60 fps needs more bits for the same quality
    if (ow, oh) != (info["width"], info["height"]):
        plan["reasons"].append(f"downscale {info['width']}x{info['height']}")
    if info["vbps"] > cap:
        plan["reasons"].append(f"{info['vbps'] / 1e6:.1f} Mbps > {cap / 1e6:.1f}")
    if info["codec"] not in CODEC_FACTOR:
        plan["reasons"].append(f"codec {info['codec']}")
    audio, abps = plan_audio(info, p["audio"])
    kept = keep_by_language(info["audio"])
    dropped_langs = sorted({a["lang"] for a in info["audio"] if a["idx"] not in kept})
    burn = burn_candidate(info, p.get("burn_subs", "none"))
    video_copy = not plan["reasons"] and not burn and not tone
    if dropped_langs:
        plan["reasons"].append("drop " + ", ".join(LANG_NAMES.get(l, l) for l in dropped_langs) + " audio")
    if any(a[1] not in ("copy", "drop") for a in audio) or (any(a[1] == "drop" for a in audio) and not dropped_langs):
        plan["reasons"].append("audio")
    # estimate: what would the video weigh after a HEVC encode at this quality?
    factor = CODEC_FACTOR.get(info["codec"], LEGACY)
    px = (ow * oh) / max(1, info["width"] * info["height"])
    vbps = min(info["vbps"] * factor * px ** 0.8, cap)
    if (info["codec"] == "hevc" and info["vbps"] <= cap and px == 1) or video_copy:
        vbps = info["vbps"]          # video stays as it is
    est = (vbps + abps) * info["duration"] / 8 + 2e6
    saving = 1 - est / max(1, info["size"])
    if burn:
        plan["reasons"].append(f"burn {burn[1]} subs")
    plan.update(out_w=ow, out_h=oh, cap=cap, audio=audio, hdr_to_sdr=bool(tone), burn_sub=burn[0] if burn else None,
                burn_kind=burn[1] if burn else None, video_copy=video_copy, est_size=int(min(est, info["size"])))
    threshold = 2 if video_copy else CFG["min_savings_percent"]     # a remux takes seconds and loses nothing
    if plan["reasons"] and (saving * 100 >= threshold or burn):
        plan["action"] = "encode"
        if video_copy:
            plan["reasons"].append("video kept as-is")
        if tone:
            plan["reasons"].append("HDR to SDR")
    elif plan["reasons"]:
        plan["reasons"].append(f"only ~{max(0, saving) * 100:.0f}% to gain")
    return plan

def all_infos(scope=""):
    for p, c in CACHE.items():
        if c.get("info") and (not scope or inside(p, scope)):
            yield c["info"]

def recommendations(scope=""):
    infos = [(i, library_of(i["path"])) for i in all_infos(scope)]
    infos = [(i, l) for i, l in infos if l]
    groups = {}
    for i, l in infos:
        g = group_key(i["path"], l)
        if g:
            groups.setdefault(g, []).append(i)
    typ = {g: typical(v) for g, v in groups.items()}
    items = {}
    for i, l in infos:
        key = item_key(i["path"], l)
        pname, p = preset_for(key, l)
        plan = evaluate(i, pname, p, typ.get(group_key(i["path"], l)))
        it = items.setdefault(key, {"key": key, "name": Path(key).name if key != i["path"] else Path(key).stem,
                                    "library": l["path"], "type": l["type"], "preset": pname, "auto_preset": auto_preset(key, l),
                                    "source": "override" if key in CFG["overrides"] else _auto(key, l)[1],
                                    "genres": sorted(item_genres(key)),
                                    "protected": any(inside(key, k) for k in CFG["protected"]),
                                    "files": [], "size": 0, "est": 0, "n_encode": 0})
        it["files"].append({"path": i["path"], "rel": str(Path(i["path"]).relative_to(key)) if key != i["path"] else Path(i["path"]).name,
                            "size": i["size"], "codec": i["codec"], "res": f"{i['width']}x{i['height']}",
                            "mbps": round(i["vbps"] / 1e6, 1), "hdr": i["hdr"], "minutes": round(i["duration"] / 60),
                            "audio": ", ".join(f"{a['lang']} {a['codec']} {a['ch']}ch" for a in i["audio"]),
                            "subs": ", ".join(f"{SUB_NAMES.get(s['codec'], s['codec'])} {s['lang']}" for s in i["subs"]),
                            "action": plan["action"], "reasons": plan["reasons"], "target": f"{plan['out_w']}x{plan['out_h']}",
                            "est": plan["est_size"] if plan["action"] == "encode" else i["size"]})
        it["size"] += i["size"]
        it["est"] += it["files"][-1]["est"]
        it["n_encode"] += plan["action"] == "encode"
    held = originals_summary()["by_key"]
    out = sorted(items.values(), key=lambda x: x["est"] - x["size"])
    for it in out:
        it["files"].sort(key=lambda f: f["rel"])
        it["held"] = held.get(it["key"], 0)
    return out

def plan_for_path(path):
    c = CACHE.get(path)
    lib = library_of(path) if c and c.get("info") else None
    if not lib:
        return None, None
    info = c["info"]
    g = group_key(path, lib)
    typ = typical([i for i in all_infos(g)]) if g else None
    pname, p = preset_for(item_key(path, lib), lib)
    return info, (evaluate(info, pname, p, typ), p)


# ---------- Plex ----------
def plex_get(path, **params):
    url = CFG["plex_url"].rstrip("/") + path + "?" + urlencode({"X-Plex-Token": CFG["plex_token"], **params})
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=60) as r:
        body = r.read()
    return json.loads(body)["MediaContainer"] if body.strip() else {}

def local_dir_index():
    """tail-of-path -> local folders, so a server's /volume1/video/TV/Show/Season 1 (or smb://nas/video/TV/Show/)
    finds \\\\nas\\video\\TV\\Show\\Season 1. Show/movie folders are indexed too, for servers that report those."""
    idx = {}
    for p in CACHE:
        dirs = {str(Path(p).parent)}
        lib = library_of(p)
        if lib:
            dirs.add(item_key(p, lib))
        for d in dirs:
            parts = Path(d).parts
            for n in range(1, min(5, len(parts)) + 1):
                idx.setdefault(tuple(x.lower() for x in parts[-n:]), set()).add(d)
    return idx

def match_dir(idx, server_file):
    parts = [x for x in server_file.replace("\\", "/").split("/") if x][:-1]
    for n in range(1, min(5, len(parts)) + 1):
        hits = idx.get(tuple(x.lower() for x in parts[-n:]))
        if not hits:
            return None
        if len(hits) == 1:
            return next(iter(hits))
    return None

# ---- genre guesses for things with no usable genres -------------------------------------------------
GUESS = {}
_TMDB_GENRES = {}

def _get_json(url, headers=None):
    req = Request(url, headers={"User-Agent": "Reclaimer (home media tool)", "Accept": "application/json", **(headers or {})})
    with urlopen(req, timeout=12) as r:
        return json.loads(r.read())

def _title_year(name):
    m = re.match(r"^(.*?)\s*[\(\[]((?:19|20)\d\d)[\)\]]", name)
    title, year = (m.group(1), int(m.group(2))) if m else (re.sub(r"[\(\[].*$", "", name), None)
    return re.sub(r"[._]+", " ", title).strip(), year

def _words(t):
    return set(re.findall(r"[a-z0-9]+", t.lower().replace("&", "and"))) - {"the", "a", "an", "and", "of"}

def _close(a, b, ya=None, yb=None):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if ya and yb and abs(ya - yb) > 1:
        return False
    small = min(wa, wb, key=len)                     # "Dark" must not match "Dark Matter"
    return len(wa & wb) / len(wa | wb) >= 0.6 or (len(small) >= 2 and (wa <= wb or wb <= wa))

def _searches(title):
    """The full name, then the part before a colon or dash: "Naruto: The Ocean Cut" also tries "Naruto"."""
    out = [title]
    short = re.split(r"\s*[:\u2013\u2014]\s*|\s+-\s+", title)[0].strip()
    if short and short != title:
        out.append(short)
    return out

def offline_guess(infos):
    """Japanese audio + styled (ASS/SSA) subtitles in most episodes is almost always anime."""
    if not infos:
        return None
    hits = sum(1 for i in infos if any(a["lang"] == "jpn" for a in i["audio"]) and any(s["codec"] in ("ass", "ssa") for s in i["subs"]))
    if hits * 2 > len(infos):
        return {"genres": ["anime"], "source": "Japanese audio with styled subtitles"}
    return None

def _tvmaze(title, year, ids):
    show = None
    for kind, key in (("imdb", "imdb"), ("tvdb", "thetvdb")):
        v = next((i.split(":", 1)[1] for i in ids if i.startswith(kind + ":")), None)
        if v:
            try:
                show = _get_json(f"https://api.tvmaze.com/lookup/shows?{key}={quote(v)}")
                break
            except Exception:
                pass
    if not show:
        for q in _searches(title):
            time.sleep(0.5)                                 # TVmaze allows ~20 requests per 10 s
            try:
                res = _get_json("https://api.tvmaze.com/search/shows?q=" + quote(q))
            except Exception:
                continue
            for r in res[:5]:
                sh = r.get("show") or {}
                y = int(sh["premiered"][:4]) if sh.get("premiered") else None
                if _close(q, sh.get("name", ""), year if q == title else None, y):
                    show = sh
                    break
            if show:
                break
    if not show:
        return None
    g = [x.lower() for x in show.get("genres") or []]
    if show.get("type") == "Animation" and show.get("language") == "Japanese":
        g.append("anime")
    if show.get("type") in ("Reality", "Talk Show", "Game Show", "News", "Sports", "Documentary"):
        g.append(show["type"].lower())
    if show.get("type") == "Animation":
        g.append("animation")
    return {"genres": sorted(set(g)), "source": "TVmaze: " + show.get("name", "")} if g else None

def _tmdb(title, year, ids, kind):
    key = CFG.get("tmdb_key", "").strip()
    if not key:
        return None
    auth = {"Authorization": "Bearer " + key} if len(key) > 40 else {}    # v4 read token or v3 api key
    def get(path, **params):
        if not auth:
            params["api_key"] = key
        return _get_json(f"https://api.themoviedb.org/3{path}?" + urlencode(params), auth)
    t = "tv" if kind == "tv" else "movie"
    if t not in _TMDB_GENRES:
        _TMDB_GENRES[t] = {g["id"]: g["name"].lower() for g in get(f"/genre/{t}/list").get("genres", [])}
    hit = None
    imdb = next((i.split(":", 1)[1] for i in ids if i.startswith("imdb:")), None)
    if imdb:
        r = get(f"/find/{imdb}", external_source="imdb_id")
        hit = (r.get(f"{t}_results") or [None])[0]
    if not hit:
        tm = next((i.split(":", 1)[1] for i in ids if i.startswith("tmdb:")), None)
        if tm:
            try:
                hit = get(f"/{t}/{tm}")
                hit["genre_ids"] = [g["id"] for g in hit.get("genres", [])]
            except Exception:
                hit = None
    if not hit:
        for q in _searches(title):
            params = {"query": q}
            if year and q == title:
                params["first_air_date_year" if t == "tv" else "year"] = year
            for r in get(f"/search/{t}", **params).get("results", [])[:5]:
                name = r.get("name") or r.get("title") or ""
                d = r.get("first_air_date") or r.get("release_date") or ""
                if _close(q, name, year if q == title else None, int(d[:4]) if d[:4].isdigit() else None):
                    hit = r
                    break
            if hit:
                break
    if not hit:
        return None
    g = [_TMDB_GENRES[t].get(i, "") for i in hit.get("genre_ids", [])]
    g = [x for x in g if x]
    if "animation" in g and hit.get("original_language") == "ja":
        g.append("anime")
    return {"genres": sorted(set(g)), "source": "TMDB: " + (hit.get("name") or hit.get("title") or "")} if g else None

def guess_genres(item, kind, infos):
    """Best guess at an item's genres: offline anime check first, then TMDB (if a key is set), then TVmaze for TV."""
    ck = item + ("|tmdb" if CFG.get("tmdb_key") else "")       # adding a TMDB key retries earlier misses
    if ck in GUESS:
        return GUESS[ck]
    g = offline_guess(infos)
    if not g and CFG.get("online_lookup"):
        title, year = _title_year(Path(item).name)
        ids = set(GENRES.get(item, {}).get("ids", []))
        for fn in ((lambda: _tmdb(title, year, ids, kind)), (lambda: _tvmaze(title, year, ids) if kind == "tv" else None)):
            try:
                g = fn()
            except Exception as e:
                print(f"genre lookup for {title} failed: {e}")
                g = None
            if g:
                break
    if g or CFG.get("online_lookup"):               # remember misses too, so they aren't looked up every time
        GUESS[ck] = g
    return g

def suggest(keys):
    out, want, by = {}, set(keys), {}
    with LOCK:
        infos = list(all_infos())
    for i in infos:
        l = library_of(i["path"])
        if l and item_key(i["path"], l) in want:
            by.setdefault(item_key(i["path"], l), []).append(i)
    for k in keys:
        lib = library_of(k)
        if not lib:
            continue
        g = guess_genres(k, lib["type"], by.get(k, []))
        if g:
            p = rule_preset(set(g["genres"]) | item_genres(k), lib["type"], k)
            if p:
                out[k] = {"preset": p, "source": g["source"], "genres": g["genres"]}
    save_json(GUESS_PATH, GUESS)
    return out

def rule_preset(genres, kind, item):
    for rule in CFG["plex_rules"] + [GENERAL_RULE]:     # everyday genres always last, even if a saved rule list lacks them
        alts = [{g.strip().lower() for g in alt.split("+") if g.strip()} for alt in rule["genre"].split("|")]
        if any(need and need <= genres for need in alts) and rule.get(kind):
            name = rule[kind]
            if name.endswith(" HD") and name[:-3] + " SD" in CFG["presets"]:
                if AUTO.get("_maxh", {}).get(item, 1080) <= 576:
                    name = name[:-3] + " SD"
            return name if name in CFG["presets"] else None
    return None

SERVERS = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby", "kodi": "Kodi"}

def servers():
    """Media servers with enough settings to talk to."""
    need = {"plex": ("plex_url", "plex_token"), "jellyfin": ("jellyfin_url", "jellyfin_key"),
            "emby": ("emby_url", "emby_key"), "kodi": ("kodi_url",)}
    return [a for a, keys in need.items() if all(CFG.get(k) for k in keys)]

def jf_request(app, path, method="GET", **params):
    """Jellyfin / Emby REST call (Emby may live under /emby)."""
    base = CFG[f"{app}_url"].rstrip("/")
    qs = "?" + urlencode(params) if params else ""
    for prefix in ("", "/emby"):
        req = Request(base + prefix + path + qs, data=b"" if method == "POST" else None, method=method,
                      headers={"X-Emby-Token": CFG[f"{app}_key"], "Accept": "application/json"})
        try:
            with urlopen(req, timeout=60) as r:
                body = r.read()
            return json.loads(body) if body.strip() else {}
        except HTTPError as e:
            if e.code != 404 or prefix:
                raise

def kodi_rpc(method, params=None):
    headers = {"Content-Type": "application/json"}
    if CFG.get("kodi_user"):
        headers["Authorization"] = "Basic " + base64.b64encode(f"{CFG['kodi_user']}:{CFG.get('kodi_password', '')}".encode()).decode()
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}).encode()
    with urlopen(Request(CFG["kodi_url"].rstrip("/") + "/jsonrpc", data=body, headers=headers), timeout=60) as r:
        d = json.loads(r.read())
    if "error" in d:
        raise ValueError(d["error"].get("message", "Kodi error"))
    return d.get("result") or {}

def server_items(app):
    """(kind, genres, path) for every show and movie a server knows. A path ending in / is a show folder."""
    if app == "plex":
        for sec in [d for d in plex_get("/library/sections").get("Directory", []) if d["type"] in ("show", "movie")]:
            kind = "tv" if sec["type"] == "show" else "movies"
            items = plex_get(f"/library/sections/{sec['key']}/all").get("Metadata", [])
            for n, m in enumerate(items, 1):
                MEDIA["msg"] = f"Plex: reading {sec['title']} {n}/{len(items)}…"
                full = (plex_get(f"/library/metadata/{m['ratingKey']}").get("Metadata") or [m])[0]
                genres = {g["tag"].lower() for g in full.get("Genre", []) or m.get("Genre", [])}
                if kind == "tv":
                    leaves = plex_get(f"/library/metadata/{m['ratingKey']}/allLeaves",
                                      **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 1}).get("Metadata", [])
                    files = [p["file"] for e in leaves for md in e.get("Media", []) for p in md.get("Part", [])]
                else:
                    files = [p["file"] for md in full.get("Media", []) or m.get("Media", []) for p in md.get("Part", [])]
                yield kind, genres, files[0] if files else ""
    elif app in ("jellyfin", "emby"):
        start = 0
        while True:
            d = jf_request(app, "/Items", Recursive="true", IncludeItemTypes="Movie,Series", Fields="Genres,Path",
                           StartIndex=start, Limit=500)
            items = d.get("Items", [])
            MEDIA["msg"] = f"{SERVERS[app]}: reading {start + len(items)}/{d.get('TotalRecordCount', '?')}…"
            for it in items:
                tv = it.get("Type") == "Series"
                path = it.get("Path") or ""
                yield ("tv" if tv else "movies"), {g.lower() for g in it.get("Genres", [])}, (path.rstrip("/\\") + "/" if tv and path else path)
            start += len(items)
            if not items or start >= d.get("TotalRecordCount", 0):
                break
    elif app == "kodi":
        MEDIA["msg"] = "Kodi: reading library…"
        for m in kodi_rpc("VideoLibrary.GetMovies", {"properties": ["genre", "file"]}).get("movies", []):
            f = m.get("file", "")
            if f.startswith("stack://"):
                f = f[8:].split(" , ")[0]
            yield "movies", {g.lower() for g in m.get("genre", [])}, f
        for t in kodi_rpc("VideoLibrary.GetTVShows", {"properties": ["genre", "file"]}).get("tvshows", []):
            yield "tv", {g.lower() for g in t.get("genre", [])}, t.get("file", "")

def media_sync():
    """Read genres from every connected media server and match their shows/movies to your folders."""
    try:
        idx = local_dir_index()
        if not idx:
            raise ValueError("scan your libraries first so their folders are known")
        found, notes = {}, []
        for app in servers():
            seen, unmatched = 0, []
            try:
                for kind, genres, path in server_items(app):
                    seen += 1
                    d = match_dir(idx, path + "x" if path.endswith(("/", "\\")) else path) if path else None
                    lib = library_of(d + os.sep + "x") if d else None
                    if not lib:
                        unmatched.append(path or "?")
                        continue
                    if genres:
                        key = item_key(os.path.join(d, "x"), lib)
                        found.setdefault(app, {})[key] = sorted(set(found.get(app, {}).get(key, [])) | genres)
                notes.append(f"{SERVERS[app]}: {seen - len(unmatched)} of {seen} matched to your folders"
                             + (f" (not matched, e.g. {unmatched[0]})" if unmatched else ""))
            except Exception as e:
                notes.append(f"{SERVERS[app]} failed: {e}")
                found.pop(app, None)
                found.setdefault("_failed", []).append(app)
        with LOCK:
            for app in servers():
                if app in found.get("_failed", []):
                    continue                           # keep what an unreachable server told us last time
                for g in GENRES.values():
                    g.pop(app, None)
                for key, g in found.get(app, {}).items():
                    GENRES.setdefault(key, {})[app] = g
            save_json(GENRES_PATH, GENRES)
            AUTO.clear()
        MEDIA["msg"] = genre_report() + ("; " + "; ".join(notes) if notes else "")
    except Exception as e:
        MEDIA["msg"] = f"failed: {e}"
    finally:
        MEDIA["running"] = False

NFO_GENRE = re.compile(r"<genre>\s*(.*?)\s*</genre>", re.I | re.S)

def nfo_file_genres(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    out = set()
    for g in NFO_GENRE.findall(text):
        for part in re.split(r"\s*[/|;,]\s*", html.unescape(g)):
            if part.strip():
                out.add(part.strip().lower())
    return out

def nfo_genres(folder, kind):
    """Genres for one show or movie folder. TV: tvshow.nfo, else the genres at least half the episode NFOs agree on."""
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    if kind == "tv":
        if "tvshow.nfo" in {n.lower() for n in names}:
            return nfo_file_genres(os.path.join(folder, next(n for n in names if n.lower() == "tvshow.nfo")))
        counts, n_files = {}, 0
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith((".", "@", "#"))]
            for f in files:
                if f.lower().endswith(".nfo") and f.lower() != "season.nfo":
                    n_files += 1
                    for g in nfo_file_genres(os.path.join(dirpath, f)):
                        counts[g] = counts.get(g, 0) + 1
        return {g for g, c in counts.items() if c * 2 >= n_files} if n_files else set()
    out = set()
    for n in names:
        if n.lower().endswith(".nfo"):
            out |= nfo_file_genres(os.path.join(folder, n))
    return out

NFO_IDS = [re.compile(r'<uniqueid[^>]*type="(tmdb|imdb|tvdb)"[^>]*>\s*([^<\s]+)\s*<', re.I),
           re.compile(r"<(tmdbid|imdbid|tvdbid)>\s*([^<\s]+)\s*<", re.I)]

NFO_NESTED = re.compile(r"<(actor|set|director|producer|credits|writer|fileinfo|ratings|episodedetails|namedseason)\b[^>]*>.*?</\1\s*>",
                        re.I | re.S)

def nfo_ids(folder, kind):
    """Database ids (tmdb/imdb/tvdb) from the show's or movie's own NFO, for duplicate matching."""
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    pick = [n for n in names if n.lower() == "tvshow.nfo"] if kind == "tv" else [n for n in names if n.lower().endswith(".nfo")]
    out = set()
    for n in pick:
        try:
            text = Path(folder, n).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # cast, crew and collection entries carry their own ids (an actor's tmdb id is in every film they're in)
        text = NFO_NESTED.sub(" ", text)
        for rx in NFO_IDS:
            for src, val in rx.findall(text):
                src, val = src.lower().replace("id", "") or "imdb", val.strip().lower()
                if src == "imdb" and not re.fullmatch(r"tt\d{5,}", val) or src != "imdb" and not re.fullmatch(r"\d+", val) \
                        or not val.strip("0t"):
                    continue                              # placeholders like 0 or tt0000000
                out.add(f"{src}:{val}")
    return out

def read_nfos(scope=""):
    for lib in CFG["libraries"]:
        if scope and not (inside(scope, lib["path"]) or inside(lib["path"], scope)):
            continue
        try:
            entries = [e for e in os.scandir(lib["path"]) if e.is_dir() and not e.name.startswith((".", "@", "#"))]
        except OSError:
            continue
        for e in entries:
            if scope and not (inside(e.path, scope) or inside(scope, e.path)):
                continue
            SCAN["phase"] = f"reading NFO files: {e.name[:40]}"
            g = nfo_genres(e.path, lib["type"])
            ids = nfo_ids(e.path, lib["type"])
            with LOCK:
                if g:
                    GENRES.setdefault(e.path, {})["nfo"] = sorted(g)
                elif e.path in GENRES:
                    GENRES[e.path].pop("nfo", None)
                if ids:
                    GENRES.setdefault(e.path, {})["ids"] = sorted(ids)
                elif e.path in GENRES:
                    GENRES[e.path].pop("ids", None)
    with LOCK:
        save_json(GENRES_PATH, GENRES)
        AUTO.clear()

def genre_report():
    counts, total, matched = {}, 0, 0
    for lib in CFG["libraries"]:
        for key in {item_key(i["path"], lib) for i in all_infos(lib["path"])}:
            total += 1
            name, src = _auto(key, lib)
            if src:
                matched += 1
                counts[name] = counts.get(name, 0) + 1
    return f"{matched} of {total} shows/movies got a preset from their genres ({', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'no rule matched'})"

def refresh_servers():
    """Ask every connected media server to pick up the changed files."""
    for app in servers():
        try:
            if app == "plex":
                for sec in plex_get("/library/sections").get("Directory", []):
                    if sec["type"] in ("show", "movie"):
                        plex_get(f"/library/sections/{sec['key']}/refresh")
            elif app in ("jellyfin", "emby"):
                jf_request(app, "/Library/Refresh", method="POST")
            else:
                kodi_rpc("VideoLibrary.Scan")
        except Exception:
            pass


# ---------- encoding ----------
PLACEBO = "libplacebo=tonemapping=bt.2390:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p"
ZSCALE = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
          "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")

def tonemap_filter():
    mode = CFG["tonemap"]
    if mode == "auto":
        mode = "libplacebo" if "libplacebo" in FILTERS else "zscale"
    return PLACEBO if mode == "libplacebo" else ZSCALE

def probe_filters():
    """Which filters exist, and whether libplacebo can actually get a GPU (needs Vulkan)."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    names = {line.split()[1] for line in out.splitlines() if line.startswith(" ") and len(line.split()) > 2}
    if "libplacebo" in names:
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=0.1,format=yuv420p10le",
                            "-vf", PLACEBO, "-f", "null", "-"], capture_output=True, text=True)
        if r.returncode != 0:
            names.discard("libplacebo")
            print("libplacebo can't open a GPU here; HDR tone-mapping will use the CPU (zscale) path.")
    return names

def build_cmd(info, plan, p, out, hwaccel=True, clip=None, preview=False):
    """clip=(start_s, length_s) cuts a piece; preview=True makes a small MP4 (first audio track as AAC, no subs)."""
    enc, tone = CFG["encoder"], plan["hdr_to_sdr"]
    ten_bit = (info["hdr"] and not tone) or bool(p.get("ten_bit"))
    denoise = {"light": "hqdn3d=1.5:1.5:6:6", "medium": "hqdn3d=3:3:9:9"}.get(p.get("denoise", "none"))
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if hwaccel and CFG["hwaccel_decode"] not in ("", "none") and not plan.get("video_copy"):
        cmd += ["-hwaccel", CFG["hwaccel_decode"]]
    if info["path"].lower().endswith(".avi"):
        cmd += ["-fflags", "+genpts"]
    burn, overlay = plan.get("burn_sub"), plan.get("burn_kind") == "image"
    if clip:
        cmd += ["-ss", f"{clip[0]:.3f}"]
    cmd += ["-i", info["path"]]
    if clip:
        cmd += ["-t", f"{clip[1]:.3f}"]
    cmd += ["-map", "[v]"] if overlay else ["-map", f"0:{info['vidx']}"]
    n = 0
    for idx, act, _ in plan["audio"]:
        if act == "drop":
            continue
        cmd += ["-map", f"0:{idx}"]
        if preview or act == "aac":
            cmd += [f"-c:a:{n}", "aac", f"-ac:a:{n}", "2", f"-b:a:{n}", "160k"]
        elif act == "eac3":
            cmd += [f"-c:a:{n}", "eac3", f"-ac:a:{n}", "6", f"-b:a:{n}", "448k"]
        else:
            cmd += [f"-c:a:{n}", "copy"]
        n += 1
        if preview:
            break
    s = 0
    for sub in info["subs"] if not preview else []:
        if sub["codec"] in MKV_SUBS and sub["idx"] != burn:
            cmd += ["-map", f"0:{sub['idx']}", f"-c:s:{s}", "srt" if sub["codec"] == "mov_text" else "copy"]
            s += 1
    if info["attachments"] and not preview:
        cmd += ["-map", "0:t", "-c:t", "copy"]
    k = int(plan["cap"] / 1000)
    if plan.get("video_copy"):
        cmd += ["-c:v:0", "copy"]
    elif enc == "hevc_nvenc":
        cmd += ["-c:v:0", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(p["cq"]), "-b:v", "0",
                "-maxrate", f"{k}k", "-bufsize", f"{2 * k}k", "-spatial-aq", "1", "-temporal-aq", "1",
                "-profile:v", "main10" if ten_bit else "main"]
    else:
        cmd += ["-c:v:0", "libx265", "-preset", "medium", "-crf", str(p["cq"] - 2),
                "-x265-params", f"vbv-maxrate={k}:vbv-bufsize={2 * k}:log-level=error"]
    pre, post = [], []   # filters before / after the subtitle overlay (subs are sized for the source frame)
    if plan.get("video_copy"):
        tone = False
    if info["field_order"] in ("tt", "bb", "tb", "bt"):
        pre.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    if tone:
        (pre if overlay else post).append(tonemap_filter())   # tone-map before burning so subs keep their colour
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    else:
        opt = {"color_primaries": "-color_primaries", "color_transfer": "-color_trc", "color_space": "-colorspace", "color_range": "-color_range"}
        for key, val in info["color"].items() if not plan.get("video_copy") else []:
            if val and val not in ("unknown", "reserved", "unspecified"):
                cmd += [opt[key], val]
    if (plan["out_w"], plan["out_h"]) != (info["width"], info["height"]):
        post.append(f"scale={plan['out_w']}:{plan['out_h']}:flags=lanczos")
    if denoise:
        (pre if overlay else post).append(denoise)    # before subtitles are burned in, so they stay sharp
    if burn is not None and not overlay:
        post.append("subtitles=filename=sub.ass:fontsdir=fonts")   # relative names: ffmpeg runs inside the work dir
    post.append("format=p010le" if ten_bit else "format=yuv420p")
    if plan.get("video_copy"):
        pass                                       # no filters on copied video; colour tags travel with the stream
    elif overlay:
        cmd += ["-filter_complex", f"[0:{info['vidx']}]{','.join(pre) or 'null'}[v0];"
                f"[v0][0:{burn}]overlay=eof_action=pass[v1];[v1]{','.join(post)}[v]"]
    else:
        cmd += ["-vf", ",".join(pre + post)]
    for t in STAT_TAGS:  # stale mkvmerge stats would make the new file report the old bitrate
        cmd += ["-metadata:s:v:0", f"{t}=", "-metadata:s:v:0", f"{t}-eng="]
    cmd += ["-metadata", f"{MARK_TAG}={plan['preset']}", "-max_muxing_queue_size", "9999"]
    hevc_out = not plan.get("video_copy") or info["codec"] == "hevc"
    cmd += (["-tag:v", "hvc1"] if hevc_out else []) + ["-movflags", "+faststart", "-f", "mp4", str(out)] if preview else ["-f", "matroska", str(out)]
    return cmd

def prepare_ass(info, plan, clip=None):
    """libass needs the track and the embedded fonts as files. Returns the work dir, or None on failure."""
    work = tempfile.mkdtemp(prefix="reclaimer-")
    os.mkdir(os.path.join(work, "fonts"))
    seek = ["-ss", f"{clip[0]:.3f}"] if clip else []   # input seeking shifts the subtitle times to match the clip
    subprocess.run(["ffmpeg", "-y", "-v", "error", *seek, "-i", info["path"], "-map", f"0:{plan['burn_sub']}", "-c:s", "ass", "sub.ass"], cwd=work)
    if info["attachments"]:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-dump_attachment:t", "", "-i", info["path"], "-t", "0", "-f", "null", "-"],
                       cwd=os.path.join(work, "fonts"), stderr=subprocess.DEVNULL)
    if os.path.exists(os.path.join(work, "sub.ass")):
        return work
    shutil.rmtree(work, ignore_errors=True)
    return None

def display_codec():
    """Near-lossless H.264 for the comparison clips: every browser plays it, and it hides nothing."""
    if CFG["encoder"] == "hevc_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "14", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p"]

class PreviewCancelled(Exception):
    pass

def make_preview(path):
    """A 20 s clip encoded exactly like the real thing, plus browser-playable copies of the original and the result
    for the before/after slider."""
    try:
        info, planp = plan_for_path(path)
        if not info:
            raise ValueError("file not in scan cache; scan first")
        plan, p = planp
        if plan["action"] != "encode":
            plan["action"] = "encode"
        clip = (max(0.0, info["duration"] / 2 - 10), min(20.0, info["duration"] or 20.0))
        PREVIEW_DIR.mkdir(exist_ok=True)
        for old in PREVIEW_DIR.glob("*"):                    # only the latest preview is kept
            try:
                old.unlink()
            except OSError:
                pass
        stem = "".join(c for c in Path(path).stem if c.isalnum() or c in " -_.")[:80]
        real, after, before = (PREVIEW_DIR / f"{stem}{sfx}.mp4" for sfx in (" (real HEVC)", ".after", ".before"))
        PREVIEW.update(step="encoding the sample like the real thing")
        work = prepare_ass(info, plan, clip) if plan.get("burn_kind") == "ass" else None
        try:
            rc, err = run_ffmpeg(build_cmd(info, plan, p, real, clip=clip, preview=True), clip[1], PREVIEW, work)
            if rc != 0 and not PREVIEW.get("cancel") and CFG["hwaccel_decode"] not in ("", "none"):
                rc, err = run_ffmpeg(build_cmd(info, plan, p, real, hwaccel=False, clip=clip, preview=True), clip[1], PREVIEW, work)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
        if PREVIEW.get("cancel"):
            raise PreviewCancelled()
        if rc != 0:
            raise ValueError(err or f"ffmpeg exit {rc}")
        out = summarize(real, ffprobe(real))
        PREVIEW.update(step="preparing the side-by-side", progress=0.0)
        base = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
        rc, err = run_ffmpeg(base + ["-i", str(real), "-map", "0:v:0", "-map", "0:a:0?"] + display_codec() +
                             ["-c:a", "copy", "-movflags", "+faststart", str(after)], clip[1], PREVIEW)
        if PREVIEW.get("cancel"):
            raise PreviewCancelled()
        if rc != 0:
            raise ValueError(err or "could not prepare the comparison clip")
        ref = []                                             # the original, framed exactly like the result
        if info["field_order"] in ("tt", "bb", "tb", "bt"):
            ref.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
        if plan["hdr_to_sdr"]:
            ref.append(tonemap_filter())
        ref.append(f"scale={out['width']}:{out['height']}:flags=lanczos")
        rc, err = run_ffmpeg(base + ["-ss", f"{clip[0]:.3f}", "-i", info["path"], "-t", f"{clip[1]:.3f}", "-map", f"0:{info['vidx']}",
                                     "-an", "-vf", ",".join(ref)] + display_codec() + ["-movflags", "+faststart", str(before)], clip[1], PREVIEW)
        if PREVIEW.get("cancel"):
            raise PreviewCancelled()
        if rc != 0:
            raise ValueError(err or "could not prepare the original clip")
        PREVIEW.update(step="measuring the difference")
        scores = None                                        # compare the sample with the original's same 20 s
        rc, err = run_quiet(["ffmpeg", "-hide_banner", "-nostats", "-i", str(real), "-i", str(before), "-filter_complex",
                             "[0:v]format=yuv420p[a];[1:v]format=yuv420p[b];[a][b]ssim", "-f", "null", "-"], slot=_PREV)
        if PREVIEW.get("cancel"):
            raise PreviewCancelled()
        m = re.search(r"SSIM Y:([\d.]+)", err)
        if m:
            scores = [float(m.group(1))]
        real_mbps = out["vbps"] / 1e6 if out else 0
        PREVIEW.update(
            result={"title": Path(path).name, "before": "/previews/" + quote(before.name), "after": "/previews/" + quote(after.name),
                    "real": "/previews/" + quote(real.name), "seconds": round(clip[1]),
                    "src": f"{info['width']}x{info['height']} {info['codec']} {info['vbps'] / 1e6:.1f} Mbps",
                    "dst": f"{out['width']}x{out['height']} hevc {real_mbps:.1f} Mbps" if out else "",
                    "saving": round(100 * (1 - real_mbps / max(0.01, info["vbps"] / 1e6))) if out else None,
                    "match": round(sum(scores) / len(scores), 3) if scores else None, "preset": plan["preset"]},
            msg="preview ready")
    except PreviewCancelled:
        PREVIEW.update(msg="cancelled", result=None)
    except Exception as e:
        PREVIEW["msg"] = f"preview failed: {e}"
    finally:
        PREVIEW.update(running=False, step="")

def run_ffmpeg(cmd, duration, item, cwd=None):
    slot = _PREV if item is PREVIEW else _PROC
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        slot["p"] = p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True, encoding="utf-8", errors="replace", cwd=cwd)
        for line in p.stdout:
            k, _, v = line.strip().partition("=")
            if k == "out_time_us" and duration and v.isdigit():
                item["progress"] = min(99.0, int(v) / 1e6 / duration * 100)
            elif k == "speed":
                item["speed"] = v
        p.wait()
        slot["p"] = None
        errf.seek(0)
        err = errf.read().strip()
    return p.returncode, err.splitlines()[-1][:200] if err else ""

# ---------- verification ----------
def run_quiet(cmd, cwd=None, slot=None):
    """Run ffmpeg, cancellable from the queue (or the preview window); returns (returncode, stderr text)."""
    slot = slot or _PROC
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        slot["p"] = p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf, cwd=cwd)
        p.wait()
        slot["p"] = None
        errf.seek(0)
        return p.returncode, errf.read()

def stream_durations(path):
    """Per-stream durations (seconds) for audio streams, from ffprobe (MKV keeps them in DURATION tags)."""
    data = ffprobe(path) or {}
    out = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        d = num(s.get("duration"))
        if not d:
            tag = next((v for k, v in (s.get("tags") or {}).items() if k.upper().startswith("DURATION")), "")
            m = re.match(r"(\d+):(\d+):([\d.]+)", tag)
            d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
        out.append(d)
    return out

def picture_check(src, out):
    """SSIM (luma) of short clips at 5 points, source vs output, after the same deinterlace/tone-map/scale."""
    ref = []
    if src["field_order"] in ("tt", "bb", "tb", "bt"):
        ref.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    if src["hdr"] and not out["hdr"]:
        ref.append(tonemap_filter())
    ref += [f"scale={out['width']}:{out['height']}:flags=bicubic", "format=yuv420p", "setpts=PTS-STARTPTS"]
    scores = []
    dur = min(src["duration"], out["duration"])
    for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
        t = max(0.0, dur * frac - 1)
        clip = min(2.0, max(0.5, dur - t - 0.1))
        rc, err = run_quiet(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{t:.3f}", "-i", src["path"], "-ss", f"{t:.3f}", "-i", out["path"],
                             "-t", f"{clip:.3f}", "-filter_complex",
                             f"[0:{src['vidx']}]{','.join(ref)}[r];[1:{out['vidx']}]format=yuv420p,setpts=PTS-STARTPTS[d];[d][r]ssim",
                             "-f", "null", "-"])
        m = re.search(r"SSIM Y:([\d.]+)", err)
        if _PROC.get("cancel"):
            return None
        scores.append(float(m.group(1)) if m else 0.0)
    return scores

def verify_output(src, out, full=True):
    """(ok, detail). src/out are summarize() results. Checks a clean decode, audio lengths and picture similarity."""
    if not out:
        return False, "output unreadable"
    if full:
        base = ["ffmpeg", "-hide_banner", "-nostats", "-v", "error"]
        hw = ["-hwaccel", CFG["hwaccel_decode"]] if CFG["hwaccel_decode"] not in ("", "none") else []
        tail = ["-i", out["path"], "-map", f"0:{out['vidx']}", "-map", "0:a?", "-f", "null", "-"]
        rc, err = run_quiet(base + hw + tail)
        if rc != 0 and hw and not _PROC.get("cancel"):
            rc, err = run_quiet(base + tail)
        if _PROC.get("cancel"):
            return False, "stopped by you"
        errors = [l for l in err.splitlines() if l.strip()]
        if rc != 0 or errors:
            return False, f"decode errors ({len(errors)}): {errors[0][:120] if errors else rc}"
    for i, d in enumerate(stream_durations(out["path"])):
        if d and d < out["duration"] - max(2.0, out["duration"] * 0.01):
            return False, f"audio track {i + 1} ends early ({d:.0f}s of {out['duration']:.0f}s)"
    scores = picture_check(src, out)
    if scores is None:
        return False, "stopped by you"
    low = min(scores)
    if low < CFG["verify_min_ssim"]:
        where = (0.1, 0.3, 0.5, 0.7, 0.9)[scores.index(low)]
        return False, f"picture doesn't match the original around {where * 100:.0f}% in (similarity {low:.2f})"
    return True, f"{'decoded cleanly, ' if full else ''}picture match {sum(scores) / len(scores):.2f} (lowest {low:.2f})"

def held_pairs():
    """(original in holding, its encoded replacement) for everything still held."""
    for lib in CFG["libraries"]:
        for p, _, kind in held_files(lib, ("originals",)):
            enc = (Path(lib["path"]) / p.relative_to(held_root(lib, "originals"))).with_suffix(".mkv")
            yield str(p), str(enc) if enc.exists() else ""

def verify_held():
    try:
        pairs = list(held_pairs())
        VERIFY.update(total=len(pairs), done=0, results=[])
        for orig, enc in pairs:
            VERIFY["msg"] = f"checking {Path(orig).name[:50]}"
            if not enc:
                res = (False, "the encoded file is missing - restore this one")
            else:
                res = verify_output(summarize(orig, ffprobe(orig)), summarize(enc, ffprobe(enc)), CFG["verify"] != "quick")
            VERIFY["results"].append({"original": orig, "encoded": enc, "ok": res[0], "detail": res[1]})
            VERIFY["done"] += 1
            if _PROC.get("cancel"):
                break
        bad = sum(1 for r in VERIFY["results"] if not r["ok"])
        VERIFY["msg"] = f"checked {VERIFY['done']} files: {VERIFY['done'] - bad} look right" + (f", {bad} need a look" if bad else "")
    except Exception as e:
        VERIFY["msg"] = f"failed: {e}"
    finally:
        VERIFY["running"] = False
        _PROC["cancel"] = False

def mark_ignored(folder, held_root):
    for f, body in ((folder / ".plexignore", "*\n"), (held_root / ".ignore", ""), (held_root / ".nomedia", "")):
        if not f.exists():
            f.write_text(body)

def encode_one(item):
    info, (plan, p) = plan_for_path(item["path"])
    if not info:
        return "failed", "file not in scan cache", 0
    if plan["action"] != "encode":
        return "skipped", "; ".join(plan["reasons"]), 0
    lib = library_of(info["path"])
    src = Path(info["path"])
    final, tmp = src.with_suffix(".mkv"), src.with_name(TMP_PREFIX + src.stem + ".mkv")
    if final != src and final.exists():
        return "skipped", f"{final.name} already exists", 0
    if shutil.disk_usage(src.parent).free < info["size"] * 1.1:
        return "skipped", "not enough free space for the temp file", 0
    item.update(progress=0.0, speed="")
    work = None
    if plan.get("burn_kind") == "ass":
        work = prepare_ass(info, plan)
        if not work:
            return "failed", "could not extract the ASS subtitle track", 0
    try:
        rc, err = run_ffmpeg(build_cmd(info, plan, p, tmp), info["duration"], item, work)
        if rc != 0 and not _PROC["cancel"] and plan["hdr_to_sdr"] and "libplacebo" in tonemap_filter():
            FILTERS.discard("libplacebo")       # GPU tone-mapper died; use the CPU path from now on
            if CFG["tonemap"] == "libplacebo":
                CFG["tonemap"] = "auto"
            item["progress"] = 0.0
            rc, err = run_ffmpeg(build_cmd(info, plan, p, tmp), info["duration"], item, work)
        if rc != 0 and not _PROC["cancel"] and CFG["hwaccel_decode"] not in ("", "none"):
            item["progress"] = 0.0
            rc, err = run_ffmpeg(build_cmd(info, plan, p, tmp, hwaccel=False), info["duration"], item, work)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    if rc != 0:
        tmp.unlink(missing_ok=True)
        return ("cancelled", "stopped by you", 0) if _PROC["cancel"] else ("failed", f"ffmpeg: {err or rc}", 0)
    out = summarize(tmp, ffprobe(tmp))
    want_audio = sum(1 for a in plan["audio"] if a[1] != "drop")
    problem = (None if out else "output unreadable") or \
        (f"duration mismatch ({out['duration']:.0f}s vs {info['duration']:.0f}s)" if out and abs(out["duration"] - info["duration"]) > max(2.0, info["duration"] * 0.01) else None) or \
        ("audio track count mismatch" if out and len(out["audio"]) != want_audio else None)
    if problem:
        tmp.unlink(missing_ok=True)
        return "failed", problem, 0
    verified = ""
    if CFG["verify"] != "off":
        item.update(speed="verifying", progress=99.0)
        ok, verified = verify_output(info, {**out, "path": str(tmp)}, CFG["verify"] == "full")
        if not ok:
            tmp.unlink(missing_ok=True)
            return ("cancelled", "stopped by you", 0) if _PROC["cancel"] else ("failed", f"check failed: {verified}", 0)
    new_size = tmp.stat().st_size
    pct = 100 * (1 - new_size / info["size"])
    if pct < (0 if plan.get("burn_sub") is not None else 1 if plan.get("video_copy") else CFG["min_savings_percent"]):
        tmp.unlink(missing_ok=True)
        return "rejected", f"only {pct:.0f}% smaller" if pct >= 0 else "came out bigger", 0
    st = src.stat()
    if CFG["originals"] == "delete":
        os.replace(tmp, final)
        if final != src:
            src.unlink()
    else:
        held_root = Path(lib["path"]) / CFG["originals_folder"]
        held = held_root / src.relative_to(lib["path"])
        held.parent.mkdir(parents=True, exist_ok=True)
        mark_ignored(held.parent, held_root)
        shutil.move(str(src), str(held))
        try:
            os.replace(tmp, final)
        except OSError:
            shutil.move(str(held), str(src))
            raise
    os.utime(final, (st.st_atime, st.st_mtime))
    CACHE.pop(info["path"], None)
    out["path"] = str(final)
    nst = final.stat()
    CACHE[str(final)] = {"size": nst.st_size, "mtime": nst.st_mtime, "info": out}
    return "done", f"{info['size'] / 1e9:.2f} GB -> {new_size / 1e9:.2f} GB (-{pct:.0f}%)" + (f"; {verified}" if verified else ""), info["size"] - new_size

def held_root(lib, kind="originals"):
    return Path(lib["path"]) / CFG["originals_folder" if kind == "originals" else "removed_folder"]

def held_files(lib, kinds=("originals", "removed")):
    for kind in kinds:
        root = held_root(lib, kind)
        if not root.is_dir():
            continue
        for dirpath, dirs, files in os.walk(root):
            for f in files:
                if not f.startswith("."):
                    p = Path(dirpath) / f
                    yield p, os.path.getsize(p), kind

def originals_summary():
    out = {"total": 0, "by_lib": {}, "by_key": {}}
    for lib in CFG["libraries"]:
        for p, size, kind in held_files(lib):
            rel = p.relative_to(held_root(lib, kind))
            key = str(Path(lib["path"]) / rel.parts[0]) if len(rel.parts) > 1 else str(Path(lib["path"]) / rel)
            out["total"] += size
            out["by_lib"][lib["path"]] = out["by_lib"].get(lib["path"], 0) + size
            out["by_key"][key] = out["by_key"].get(key, 0) + size
    return out

def restore_originals(key):
    """Put a show's or movie's original files back and remove the encoded versions."""
    lib = library_of(key)
    if not lib:
        raise ValueError("not inside a library")
    n = 0
    for p, _, kind in list(held_files(lib)):
        dest = Path(lib["path"]) / p.relative_to(held_root(lib, kind))
        if not inside(str(dest), key):
            continue
        if kind == "originals":                     # replaced by an encode: take the encoded version out again
            encoded = dest.with_suffix(".mkv")
            if encoded.exists():
                encoded.unlink()
            CACHE.pop(str(encoded), None)
        elif dest.exists():                          # removed duplicate/junk whose spot is taken: leave it held
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        CACHE.pop(str(dest), None)
        STATE.pop(str(dest), None)
        n += 1
    for kind in ("originals", "removed"):           # drop emptied folders
        root = held_root(lib, kind)
        for dirpath, dirs, files in os.walk(root, topdown=False):
            if dirpath != str(root) and all(f.startswith(".") for f in os.listdir(dirpath)):
                shutil.rmtree(dirpath, ignore_errors=True)
    save_cache()
    save_json(STATE_PATH, STATE)
    return n

def delete_originals(lib_path=""):
    freed = 0
    for lib in CFG["libraries"]:
        if lib_path and norm(lib["path"]) != norm(lib_path):
            continue
        freed += sum(size for _, size, _ in held_files(lib))
        shutil.rmtree(held_root(lib, "originals"), ignore_errors=True)
        shutil.rmtree(held_root(lib, "removed"), ignore_errors=True)
    return freed

def log_row(row):
    new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "file", "status", "detail", "bytes_saved", "minutes"])
        w.writerow(row)

_SESS = {"t": 0, "busy": False, "where": ""}

def media_streaming():
    if not servers():
        return False
    if time.time() - _SESS["t"] > 60:
        poll_sessions()
    return _SESS["busy"]

def blocked_reason():
    """Why encoding should wait right now, or '' if it may go ahead."""
    if QUEUE["force"]:
        return ""
    w = CFG["window"]
    if w.get("enabled"):
        h, a, b = time.localtime().tm_hour, int(w["start"]), int(w["end"])
        inside_window = a <= h < b if a < b else (h >= a or h < b)
        if not inside_window:
            return f"waiting for the encode window ({a:02d}:00-{b:02d}:00)"
    if CFG["pause_when_streaming"] and media_streaming():
        return f"paused: someone is watching {_SESS.get('where') or 'something'}"
    return ""

def worker():
    try:
        while not QUEUE["stop"]:
            item = next((i for i in QUEUE["items"] if i["status"] == "queued"), None)
            if not item:
                break
            while not QUEUE["stop"] and (why := blocked_reason()):
                QUEUE["waiting"] = why
                time.sleep(30)
            QUEUE["waiting"] = ""
            if QUEUE["stop"]:
                break
            item["status"] = "encoding"
            QUEUE["current"] = item["path"]
            _PROC["cancel"] = False
            t0 = time.time()
            try:
                status, detail, saved = encode_one(item)
            except Exception as e:
                status, detail, saved = "failed", str(e), 0
            mins = (time.time() - t0) / 60
            item.update(status=status, detail=detail, saved=saved, minutes=round(mins, 1), progress=100.0 if status == "done" else 0.0)
            QUEUE["saved"] += saved
            log_row([time.strftime("%Y-%m-%d %H:%M"), item["path"], status, detail, saved, f"{mins:.1f}"])
            if status in ("failed", "rejected"):
                STATE[item["path"]] = {"size": CACHE.get(item["path"], {}).get("size"), "reason": f"{status}: {detail}"}
                save_json(STATE_PATH, STATE)
            save_cache()
    finally:
        QUEUE.update(running=False, stop=False, current=None, waiting="", force=False)
        if CFG["plex_refresh"] and servers() and QUEUE["saved"]:
            refresh_servers()
        arr_rescan([i["path"] for i in QUEUE["items"] if i["status"] == "done"])

def start_queue(force=False):
    if not QUEUE["running"]:
        QUEUE.update(running=True, stop=False, force=force)
        threading.Thread(target=worker, daemon=True).start()

def auto_loop():
    """Auto mode: every interval, rescan everything, queue whatever is recommended, and run it (schedule permitting)."""
    last = 0
    while True:
        time.sleep(60)
        if MISSING or not CFG["auto_mode"] or SCAN["running"] or QUEUE["running"] or time.time() - last < CFG["auto_interval_min"] * 60:
            continue
        last = time.time()
        auto_tick()

def auto_tick():
    if True:
        SCAN.update(running=True, done=0, total=0, scope="", error="")
        run_scan("", False)
        with LOCK:
            have = {i["path"] for i in QUEUE["items"] if i["status"] in ("queued", "encoding")}
            QUEUE["items"] = [i for i in QUEUE["items"] if i["status"] in ("queued", "encoding")][-50:] + \
                             [i for i in QUEUE["items"] if i["status"] not in ("queued", "encoding")][-200:]
            for r in recommendations():
                for f in r["files"]:
                    if f["action"] == "encode" and f["path"] not in have:
                        QUEUE["items"].append({"path": f["path"], "status": "queued", "detail": "auto", "progress": 0.0, "speed": "", "saved": 0})
            if any(i["status"] == "queued" for i in QUEUE["items"]):
                start_queue()


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        try:
            self._send(obj, code, ctype)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass                                     # the browser closed the connection; nothing to tell it

    def send_file(self, f, ctype):
        """Serve a file with byte-range support, so the browser can seek and loop the preview videos."""
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
            end = min(end, size - 1)
        try:
            self.send_response(206 if m and (m.group(1) or m.group(2)) else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if self.headers.get("Range"):
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _send(self, obj, code, ctype):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        if path == "/":
            if MISSING:
                page = SETUP_HTML.replace("__MISSING__", " and ".join(MISSING)).replace("__HERE__", str(HERE))
                return self.send(page.encode(), ctype="text/html; charset=utf-8")
            return self.send((ASSETS / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        if path == "/favicon.svg":
            st = q.get("s") if q.get("s") in ICON_COLORS else icon_state()
            return self.send(icon_svg(st).encode(), ctype="image/svg+xml")
        if path.startswith("/previews/"):
            f = PREVIEW_DIR / unquote(path[10:]).replace("/", "").replace("\\", "")
            if f.is_file():
                return self.send_file(f, "audio/mp4" if f.suffix == ".m4a" else "video/mp4")
        if path == "/api/ping":
            return self.send({"app": "reclaimer", "version": VERSION})
        if path == "/api/state":
            with LOCK:
                return self.send({"config": CFG, "scan": SCAN, "queue": QUEUE, "media": MEDIA, "preview": PREVIEW, "cached": len(CACHE),
                                  "arr": ARR, "verify": VERIFY, "junk": {"running": JUNK["running"], "msg": JUNK["msg"], "items": JUNK["items"],
                                                       "cats": {k: {"label": v[0], "default": v[1]} for k, v in JUNK_CATS.items()}},
                                  "encoders": ["hevc_nvenc", "libx265"], "defaultPresets": list(DEFAULT_PRESETS), "defaultPresetValues": DEFAULT_PRESETS,
                                  "presetDescs": PRESET_DESCRIPTIONS})
        if path == "/api/recommend":
            with LOCK:
                return self.send(recommendations(unquote(q.get("scope", ""))))
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}") or b"{}")
        if self.path == "/api/quit":             # answered without the lock so a busy copy still responds
            if QUEUE["current"] or VERIFY["running"]:
                return self.send({"error": "busy encoding"})
            self.send({"ok": True})
            return shutdown_app()
        if self.path == "/api/suggest":                # online lookups take a while; keep the page responsive
            try:
                return self.send(suggest(body.get("keys") or []))
            except Exception as e:
                return self.send({"error": str(e)}, 400)
        if self.path in ("/api/track_sample", "/api/relabel_track"):   # can take a while on a NAS; don't freeze the page
            try:
                return self.send(track_sample(body.get("path"), int(body.get("idx"))) if self.path == "/api/track_sample"
                                 else relabel_track(body.get("path"), int(body.get("idx")), body.get("lang")))
            except Exception as e:
                return self.send({"error": str(e)}, 400)
        with LOCK:
            try:
                self.send(self.api_post(self.path, body))
            except Exception as e:
                self.send({"error": str(e)}, 400)

    def api_post(self, path, b):
        if path == "/api/config":
            AUTO.clear()
            for k in ("libraries", "presets", "overrides", "encoder", "originals", "min_savings_percent", "show_rule", "tonemap",
                      "hwaccel_decode", "plex_url", "plex_token", "plex_rules", "plex_refresh", "jellyfin_url", "jellyfin_key",
                      "emby_url", "emby_key", "kodi_url", "kodi_user", "kodi_password", "burn_subs_lang",
                      "window", "pause_when_streaming", "auto_mode", "auto_interval_min", "protected",
                      "audio_lang_filter", "audio_keep_langs", "audio_keep_unknown", "audio_keep_default", "notify",
                      "sonarr_url", "sonarr_key", "radarr_url", "radarr_key", "arr_guard", "arr_rescan", "verify", "verify_min_ssim",
                      "setup_done", "setup_presets", "setup_answers", "port", "online_lookup", "tmdb_key"):
                if k in b:
                    CFG[k] = b[k]
            save_json(CONFIG_PATH, CFG)
            return {"ok": True}
        if path == "/api/languages":
            return language_summary()
        if path == "/api/language_items":
            return language_items(lang_code(b.get("lang")))
        if path == "/api/recheck":
            if check_tools():
                init_engine()
            return {"ok": not MISSING, "missing": MISSING}
        if path == "/api/protect":
            keys = [k for k in CFG["protected"] if norm(k) != norm(b["key"])]
            CFG["protected"] = keys + ([b["key"]] if b.get("on") else [])
            save_json(CONFIG_PATH, CFG)
            return {"ok": True}
        if path == "/api/override":
            if b.get("preset"):
                CFG["overrides"][b["key"]] = b["preset"]
            else:
                CFG["overrides"].pop(b["key"], None)
            save_json(CONFIG_PATH, CFG)
            return {"ok": True}
        if path == "/api/media/sync":
            if MEDIA["running"]:
                return {"error": "already syncing"}
            if not servers():
                return {"error": "add a media server first (Plex, Jellyfin, Emby or Kodi)"}
            MEDIA.update(running=True, msg="connecting…")
            threading.Thread(target=media_sync, daemon=True).start()
            return {"ok": True}
        if path == "/api/scan":
            if SCAN["running"]:
                return {"error": "scan already running"}
            SCAN.update(running=True, done=0, total=0, scope=b.get("scope", ""), error="")
            threading.Thread(target=run_scan, args=(b.get("scope", ""), b.get("force", False)), daemon=True).start()
            return {"ok": True}
        if path == "/api/queue/add":
            have = {i["path"] for i in QUEUE["items"]}
            for p in b.get("paths", []):
                if p not in have:
                    QUEUE["items"].append({"path": p, "status": "queued", "detail": "", "progress": 0.0, "speed": "", "saved": 0})
            return {"ok": True}
        if path == "/api/queue/start":
            start_queue(bool(b.get("force")))
            return {"ok": True}
        if path == "/api/preview/cancel":
            if PREVIEW["running"]:
                PREVIEW["cancel"] = True
                if _PREV["p"]:
                    _PREV["p"].kill()
            return {"ok": True}
        if path == "/api/preview":
            if PREVIEW["running"]:
                return {"error": "a preview is already being made"}
            PREVIEW.update(running=True, cancel=False, msg="making preview…", result=None, progress=0.0, step="starting")
            threading.Thread(target=make_preview, args=(b["path"],), daemon=True).start()
            return {"ok": True}
        if path == "/api/arr/sync":
            if ARR["running"]:
                return {"error": "already syncing"}
            ARR.update(running=True, msg="connecting…")
            threading.Thread(target=arr_sync, daemon=True).start()
            return {"ok": True}
        if path == "/api/verify_held":
            if QUEUE["running"] or VERIFY["running"]:
                return {"error": "wait for the queue to finish first" if QUEUE["running"] else "already checking"}
            VERIFY.update(running=True, msg="starting…", results=[])
            threading.Thread(target=verify_held, daemon=True).start()
            return {"ok": True}
        if path == "/api/dupes":
            return find_duplicates()
        if path == "/api/dupes/remove":
            moved, freed, done = remove_duplicates(b.get("paths", []))
            arr_rescan(done)
            return {"ok": True, "moved": moved, "freed": freed}
        if path == "/api/junk/find":
            if not JUNK["running"]:
                JUNK.update(running=True, msg="looking for junk…")
                threading.Thread(target=find_junk, daemon=True).start()
            return {"ok": True}
        if path == "/api/junk/remove":
            known = {j["path"] for j in JUNK["items"]}
            moved, freed, done = remove_paths([p for p in b.get("paths", []) if p in known], "junk")
            arr_rescan(done)
            JUNK["items"] = [j for j in JUNK["items"] if os.path.exists(j["path"])]
            JUNK["msg"] = f"{len(JUNK['items'])} items left, {sum(j['size'] for j in JUNK['items']) / 1e9:.2f} GB"
            return {"ok": True, "moved": moved, "freed": freed}
        if path == "/api/health":
            return {"history": history(), "transcodes": transcode_list(), "arr": arr_summary(), "arr_msg": ARR["msg"],
                    "arr_items": len(ARR_MAP)}
        if path == "/api/transcodes/clear":
            TRANSCODES.clear()
            save_json(TRANSCODES_PATH, TRANSCODES)
            return {"ok": True}
        if path == "/api/originals":
            return originals_summary()
        if path == "/api/originals/restore":
            if QUEUE["running"]:
                return {"error": "stop the queue first"}
            return {"ok": True, "restored": restore_originals(b["key"])}
        if path == "/api/originals/delete":
            if QUEUE["running"] or VERIFY["running"]:
                return {"error": "stop the queue first" if QUEUE["running"] else "wait for the check to finish"}
            return {"ok": True, "freed": delete_originals(b.get("library", ""))}
        if path == "/api/queue/stop":          # finish the current file, then stop
            QUEUE["stop"] = True
            return {"ok": True}
        if path == "/api/queue/cancel":        # kill the current file too
            QUEUE["stop"] = True
            _PROC["cancel"] = True
            if _PROC["p"]:
                _PROC["p"].kill()
            return {"ok": True}
        if path == "/api/queue/clear":
            QUEUE["items"] = [i for i in QUEUE["items"] if i["status"] == "encoding"]
            return {"ok": True}
        if path == "/api/retry":                # forget failures/rejections so they can be tried again
            for p in b.get("paths", []):
                STATE.pop(p, None)
            save_json(STATE_PATH, STATE)
            return {"ok": True}
        raise ValueError("unknown endpoint")


MISSING = []
SETUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Reclaimer setup</title>
<style>body{background:#15171c;color:#d8dbe2;font:15px/1.55 system-ui,Segoe UI,sans-serif;max-width:720px;margin:40px auto;padding:0 20px}
a{color:#5aa9ff}code{background:#262a33;padding:1px 5px;border-radius:4px}button{background:#5aa9ff;color:#081527;border:0;border-radius:5px;padding:8px 16px;font-weight:600;cursor:pointer}
li{margin-bottom:8px}#msg{color:#e3a84f;margin-left:10px}</style></head><body>
<h2>Reclaimer needs ffmpeg</h2>
<p>ffmpeg is the free program that does the actual video work (reading files, encoding on your GPU). Reclaimer can't scan or encode without it. Missing: <b>__MISSING__</b></p>
<h3>Easiest: install with winget</h3>
<p>Open a terminal and run <code>winget install Gyan.FFmpeg</code>, then click <b>Check again</b>.</p>
<h3>Or download it yourself</h3>
<ol><li>Go to <a href="https://www.gyan.dev/ffmpeg/builds/" target="_blank">gyan.dev/ffmpeg/builds</a> (the Windows builds linked from <a href="https://ffmpeg.org/download.html" target="_blank">ffmpeg.org</a>).</li>
<li>Under <b>release builds</b>, download <code>ffmpeg-release-full.7z</code> (open it with 7-Zip). The full build has everything Reclaimer uses.</li>
<li>From its <code>bin</code> folder, copy <code>ffmpeg.exe</code> and <code>ffprobe.exe</code> into this folder:<br><code>__HERE__</code></li>
<li>Click <b>Check again</b>.</li></ol>
<p><button onclick="fetch('/api/recheck',{method:'POST',body:'{}'}).then(r=>r.json()).then(r=>{if(r.ok)location.reload();else document.getElementById('msg').textContent='still missing: '+r.missing.join(', ')})">Check again</button><span id="msg"></span></p>
</body></html>"""

def check_tools():
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"   # where winget puts ffmpeg
    if links.is_dir() and str(links) not in os.environ["PATH"]:
        os.environ["PATH"] += os.pathsep + str(links)
    MISSING[:] = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    return not MISSING

def init_engine():
    global FILTERS
    FILTERS = probe_filters()
    if "zscale" not in FILTERS and "libplacebo" not in FILTERS:
        print("WARNING: this ffmpeg has no tone-mapping filter; HDR files will fail. Install the 'full' Gyan build.")

class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"   # on Windows, reuse would let two programs share one port silently
    daemon_threads = True

VERSION = str(int(os.path.getmtime(sys.executable if getattr(sys, "frozen", False) else __file__)))

def popup(text):
    """Show a message even when running without a console (pythonw)."""
    print(text)
    if os.name == "nt" and sys.stdout is not None and not sys.stdout.isatty():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "Reclaimer", 0x40)
        except Exception:
            pass

def running_reclaimer(port):
    """Info about a Reclaimer already on this port, or None. Only a program that answers /api/ping as
    "reclaimer" counts; anything else (your other tools included) is never contacted further."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            d = json.loads(r.read())
            return d if isinstance(d, dict) and d.get("app") == "reclaimer" else None
    except Exception:
        return None

DEFAULT_PORT = 36597
OLD_PORTS = (8765,)                               # where earlier versions ran, so a leftover copy is still found

def port_taken(port):
    """Instant check that something is listening, so free ports are never pinged (on Windows a ping to a
    closed port takes ~2 s, and checking 60 of them made startup hang)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()

def take_over(port, no_browser):
    """Another Reclaimer answers on this port: open it if it's this version, otherwise ask it to quit.
    Returns once it has quit; exits if it can't."""
    other = running_reclaimer(port)
    if not other:
        return
    if other.get("version") == VERSION:
        print(f"Reclaimer is already running at http://localhost:{port} - opening it.")
        if not no_browser:
            webbrowser.open(f"http://localhost:{port}")
        sys.exit(0)
    try:
        req = Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as r:
            answer = json.loads(r.read())
    except Exception:
        answer = {"error": "old"}
    if answer.get("ok"):
        for _ in range(40):                      # wait for it to let go of the port
            time.sleep(0.5)
            if not running_reclaimer(port):
                time.sleep(0.5)
                return
    busy = "busy" in str(answer.get("error", ""))
    popup("An older copy of Reclaimer is still running" + (" and is in the middle of encoding, so it was left alone. "
          "Quit it from its tray icon when it's done, then start Reclaimer again." if busy else
          ". Quit it from its tray icon, then start Reclaimer again."))
    if not no_browser:
        webbrowser.open(f"http://localhost:{port}")
    sys.exit(0)

def open_server(first_port, no_browser):
    """Bind the first free port from first_port up, after taking over from any other copy of Reclaimer."""
    ranges = {p for base in {first_port, DEFAULT_PORT, *OLD_PORTS} for p in range(base, base + 20)}
    for port in sorted(ranges):
        if port_taken(port):
            take_over(port, no_browser)
    for port in range(first_port, first_port + 20):
        try:
            return Server(("127.0.0.1", port), Handler), port
        except OSError:
            print(f"port {port} is in use by another program, trying {port + 1}")
    sys.exit(f"no free port between {first_port} and {first_port + 19}; start with --port N")

def shutdown_app():
    """Stop everything and exit (used by the tray's Quit and by a newer copy taking over)."""
    if _PROC["p"]:
        _PROC["cancel"] = True
        _PROC["p"].kill()
    if TRAY.get("icon"):
        try:
            TRAY["icon"].stop()
        except Exception:
            pass
    threading.Timer(1.0, lambda: os._exit(0)).start()

# ---------- Sonarr / Radarr ----------
def arr_request(app, path, body=None):
    url = CFG[f"{app}_url"].rstrip("/") + "/api/v3" + path
    req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Api-Key": CFG[f"{app}_key"], "Accept": "application/json", "Content-Type": "application/json"})
    with urlopen(req, timeout=60) as r:
        data = r.read()
    return json.loads(data) if data.strip() else {}

def arr_apps():
    return [a for a in ("sonarr", "radarr") if CFG[f"{a}_url"] and CFG[f"{a}_key"]]

def cutoff_resolution(profile):
    """Resolution (720, 1080, 2160...) of a quality profile's cutoff."""
    cut = profile.get("cutoff")
    def res(item):
        return (item.get("quality") or {}).get("resolution", 0) or max((res(i) for i in item.get("items", [])), default=0)
    def find(items):
        for it in items:
            if (it.get("items") and it.get("id") == cut) or (not it.get("items") and (it.get("quality") or {}).get("id") == cut):
                return it
            hit = find(it.get("items", []))
            if hit:
                return hit
        return None
    hit = find(profile.get("items", []))
    return res(hit) if hit else 0

def tail_match(remote_path, candidates):
    """Pick the local folder whose trailing path parts best match a path as Sonarr/Radarr see it."""
    rparts = [p.lower() for p in remote_path.replace("\\", "/").split("/") if p]
    best, best_n, tie = None, 0, False
    for c in candidates:
        cparts = [p.lower() for p in Path(c).parts]
        n = 0
        while n < min(len(rparts), len(cparts)) and rparts[-1 - n] == cparts[-1 - n].strip("\\/"):
            n += 1
        if n > best_n:
            best, best_n, tie = c, n, False
        elif n == best_n and n:
            tie = True
    return None if tie or not best_n else best

def known_items():
    out = {}
    for info in all_infos():
        lib = library_of(info["path"])
        if lib:
            out.setdefault(item_key(info["path"], lib), lib)
    return out

def arr_sync():
    try:
        items = known_items()
        by_name = {}
        for k in items:
            by_name.setdefault(Path(k).name.lower(), []).append(k)
        new_map, msgs = {}, []
        for app in arr_apps():
            ARR["msg"] = f"reading {app.title()}…"
            profiles = {p["id"]: p for p in arr_request(app, "/qualityprofile")}
            entries = arr_request(app, "/series" if app == "sonarr" else "/movie")
            matched = ambiguous = 0
            for e in entries:
                rpath = e.get("path") or ""
                cands = [k for k in by_name.get(rpath.replace("\\", "/").rstrip("/").split("/")[-1].lower(), [])
                         if items[k]["type"] == ("tv" if app == "sonarr" else "movies")]
                key = cands[0] if len(cands) == 1 else tail_match(rpath, cands)
                if not key:
                    ambiguous += bool(cands)
                    continue
                prof = profiles.get(e.get("qualityProfileId"), {})
                new_map[key] = {"app": app, "id": e["id"], "title": e.get("title", ""), "profile": prof.get("name", "?"),
                                "cutoff": cutoff_resolution(prof), "upgrade": prof.get("upgradeAllowed", True),
                                "monitored": e.get("monitored", True)}
                matched += 1
            msgs.append(f"{app.title()}: {matched} of {len(entries)} matched to your folders" + (f" ({ambiguous} ambiguous)" if ambiguous else ""))
        with LOCK:
            ARR_MAP.clear()
            ARR_MAP.update(new_map)
            save_json(ARR_PATH, ARR_MAP)
            AUTO.clear()
        ARR["msg"] = "; ".join(msgs) or "add a Sonarr or Radarr URL and API key first"
    except Exception as e:
        ARR["msg"] = f"failed: {e}"
    finally:
        ARR["running"] = False

def arr_floor(path):
    """(resolution, 'Sonarr') if Sonarr/Radarr would re-download a copy below that resolution, else None."""
    if not CFG["arr_guard"] or not ARR_MAP:
        return None
    lib = library_of(path)
    a = ARR_MAP.get(item_key(path, lib)) if lib else None
    if a and a["monitored"] and a["upgrade"] and a["cutoff"]:
        return a["cutoff"], a["app"].title()
    return None

def arr_rescan(paths):
    if not CFG["arr_rescan"]:
        return
    done = set()
    for p in paths:
        lib = library_of(p)
        a = ARR_MAP.get(item_key(p, lib)) if lib else None
        if a and (a["app"], a["id"]) not in done:
            done.add((a["app"], a["id"]))
            try:
                arr_request(a["app"], "/command", {"name": "RescanSeries", "seriesId": a["id"]} if a["app"] == "sonarr"
                            else {"name": "RescanMovie", "movieId": a["id"]})
            except Exception:
                pass

def arr_summary():
    """Files whose downscale was limited by a Sonarr/Radarr cutoff, grouped by profile."""
    out = {}
    for r in recommendations():
        a = ARR_MAP.get(r["key"])
        n = sum(1 for f in r["files"] if any(x.startswith("kept at ") for x in f["reasons"]))
        if a and n:
            e = out.setdefault((a["app"], a["profile"], a["cutoff"]), {"app": a["app"].title(), "profile": a["profile"],
                                                                       "cutoff": a["cutoff"], "items": 0, "files": 0})
            e["items"] += 1
            e["files"] += n
    return list(out.values())


# ---------- removing duplicates and junk ----------
def remove_paths(paths, reason):
    """Move files/folders into the library's removed folder (restorable until deleted)."""
    moved, freed, done = 0, 0, []
    for p in paths:
        lib = library_of(p)
        if not lib or not os.path.exists(p) or is_protected(p) or p == QUEUE["current"]:
            continue
        dest = held_root(lib, "removed") / Path(p).relative_to(lib["path"])
        if dest.exists():
            continue
        size = sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file()) if os.path.isdir(p) else os.path.getsize(p)
        dest.parent.mkdir(parents=True, exist_ok=True)
        mark_ignored(dest.parent, held_root(lib, "removed"))
        shutil.move(p, str(dest))
        for k in [k for k in CACHE if inside(k, p)]:
            CACHE.pop(k, None)
        QUEUE["items"] = [i for i in QUEUE["items"] if not (i["status"] == "queued" and inside(i["path"], p))]
        log_row([time.strftime("%Y-%m-%d %H:%M"), p, "removed", reason, size, "0"])
        moved, freed = moved + 1, freed + size
        done.append(p)
    save_cache()
    AUTO.clear()
    return moved, freed, done

EP_RE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?:[ ._-]?-?[ ._-]?[Ee](\d{1,3}))?")
PART_RE = re.compile(r"(cd|disc|disk|part|pt)[ ._-]?\d", re.I)
EXTRA_SUFFIX = re.compile(r"-(trailer|featurette|behindthescenes|deleted|interview|scene|short|other)$", re.I)
EXTRA_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "shorts", "trailers", "other", "sample", "samples"}

def title_keys(key):
    """Identity keys for a show/movie folder: ids from its NFO, plus normalised 'title|year'."""
    name = Path(key).name
    keys = {f"id:{i}" for i in GENRES.get(key, {}).get("ids", [])}
    keys |= {f"id:{m[0].lower()}:{m[1].lower()}" for m in re.findall(r"\{(tmdb|tvdb|imdb)-([^}]+)\}", name, re.I)}
    year = (re.findall(r"\((\d{4})\)", name) or [""])[-1]
    t = re.sub(r"\(\d{4}\)|\[[^\]]*\]|\{[^}]*\}", " ", name).strip().lower()
    t = re.sub(r"^(.*?),\s*(the|a|an)\s*$", r"\2 \1", t)          # "Matrix, The" -> "the matrix"
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    t = re.sub(r"^(the|a|an) ", "", t)
    if t:
        tv = (library_of(key) or {}).get("type") == "tv"
        keys.add(f"name:{t}" if tv else f"name:{t}|{year}")    # shows: title only (folders often differ by year)
    return keys

def video_rank(f):
    a = ARR_MAP.get(f["key"])
    return (bool(a), tier_of(f["w"], f["h"]), f["size"])

def find_duplicates():
    items = known_items()
    parent = {k: k for k in items}
    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    keys = {k: {lib["type"] + ":" + tk for tk in title_keys(k)} for k, lib in items.items()}
    uses = {}
    for ks in keys.values():
        for tk in ks:
            uses[tk] = uses.get(tk, 0) + 1
    year = lambda k: (re.findall(r"\((\d{4})\)", Path(k).name) or [""])[-1]
    def words(k):
        n = next((t.split(":", 1)[1][5:].split("|")[0] for t in keys[k] if t.split(":", 1)[1].startswith("name:")), "")
        return {w for w in n.split() if w not in ("the", "a", "an", "of", "and")}
    def similar(a, b):                                 # most words of the shorter title appear in the other
        wa, wb = words(a), words(b)
        return bool(wa and wb) and len(wa & wb) >= 0.75 * len(wa | wb)
    seen = {}
    for k, lib in items.items():                       # union shows/movies that share an id or title+year
        for tk in keys[k]:
            if uses[tk] > 3:                           # one key linking many titles is a bad key, not a duplicate
                continue
            if tk in seen and ":id:" in tk and ((year(k) and year(seen[tk]) and year(k) != year(seen[tk])) or not similar(k, seen[tk])):
                continue                               # an id alone isn't trusted when the years or titles disagree
            if tk in seen:
                parent[root(k)] = root(seen[tk])
            else:
                seen[tk] = k
    files = {}
    for info in all_infos():
        lib = library_of(info["path"])
        if not lib or is_protected(info["path"]):
            continue
        k = item_key(info["path"], lib)
        name = Path(info["path"]).stem
        if EXTRA_SUFFIX.search(name) or any(d.lower() in EXTRA_DIRS for d in Path(info["path"]).relative_to(k).parts[:-1]) \
                or re.search(r"(^|[\W_])sample([\W_]|$)", name, re.I):
            continue
        f = {"path": info["path"], "key": k, "lib": lib["path"], "type": lib["type"], "size": info["size"], "w": info["width"],
             "h": info["height"], "codec": info["codec"], "mbps": round(info["vbps"] / 1e6, 1), "minutes": round(info["duration"] / 60)}
        if lib["type"] == "tv":
            m = EP_RE.search(name)
            if not m or m.group(3):                    # unparseable or multi-episode file: never call it a duplicate
                continue
            gk = ("tv", root(k), int(m.group(1)), int(m.group(2)))
            fy = re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", name[:m.start()])
            f["year"] = year(k) or (fy[-1] if fy else "")
            f["ep"] = episode_words(name[m.end():])
        else:
            if PART_RE.search(name):                   # cd1/cd2 parts belong together
                continue
            gk = ("movies", root(k))
        files.setdefault(gk, []).append(f)
    groups = []
    for gk, all_fs in files.items():
      for fs in (cluster_episodes(all_fs) if gk[0] == "tv" else [all_fs]):
        if len(fs) < 2:
            continue
        if gk[0] == "movies" and (len(fs) > 4 or len({f["key"] for f in fs}) == 1 and len(fs) > 2):
            continue                                   # too many copies to be real duplicates: something matched wrongly
        fs.sort(key=video_rank, reverse=True)
        for i, f in enumerate(fs):
            f.pop("ep", None)                          # internal matching data, not for the page
            f["keep"] = i == 0
            f["managed"] = bool(ARR_MAP.get(f["key"]))
        show = Path(fs[0]["key"]).name
        title = f"{show} S{gk[2]:02d}E{gk[3]:02d}" if gk[0] == "tv" else show
        groups.append({"id": "|".join(map(str, gk)), "title": title, "type": gk[0], "files": fs,
                       "savings": sum(f["size"] for f in fs[1:])})
    groups.sort(key=lambda g: -g["savings"])
    return groups

QUALITY_CUT = re.compile(r"[\[(]|\b(?:480|576|720|1080|2160)[pi]\b|\b(?:web|webrip|web-dl|webdl|bluray|bdrip|hdtv|dvdrip|x26[45]|h\.?26[45]|hevc|amzn|nf|dsnp|hulu)\b", re.I)

def episode_words(rest):
    """Episode title words after 'SxxEyy' (quality tags dropped), split into words and numbers; None if there's no title."""
    rest = QUALITY_CUT.split(rest, 1)[0]
    w = re.sub(r"[^a-z0-9]+", " ", rest.lower()).split()
    w = [x for x in w if x not in ("the", "a", "an", "of", "and", "proper", "repack")]
    return (frozenset(x for x in w if not x.isdigit()), frozenset(x for x in w if x.isdigit())) if w else None

def same_episode(a, b):
    if a.get("year") and b.get("year") and a["year"] != b["year"]:
        return False                                   # Rugrats (1991) vs Rugrats (2021)
    ea, eb = a.get("ep"), b.get("ep")
    if not ea or not eb:
        return True                                    # no title to compare: trust the episode number
    if ea[1] != eb[1]:
        return False                                   # "Part 1" vs "Part 2"
    return not (ea[0] or eb[0]) or len(ea[0] & eb[0]) >= 0.5 * max(1, min(len(ea[0]), len(eb[0])))

def cluster_episodes(fs):
    """Split files sharing an SxxEyy into groups that really are the same episode."""
    out = []
    for f in fs:
        for c in out:
            if all(same_episode(f, g) for g in c):
                c.append(f)
                break
        else:
            out.append([f])
    return out

def remove_duplicates(paths):
    """Remove the given copies, refusing any group where no copy would be left."""
    want = set(paths)
    ok = []
    for g in find_duplicates():
        gp = [f["path"] for f in g["files"]]
        hit = [p for p in gp if p in want]
        if hit and len(hit) < len(gp):
            ok += hit
    return remove_paths(ok, "duplicate")

JUNK_CATS = {
    "sample": ("Sample clips", True), "partial": ("Unfinished downloads and stale temp files", True),
    "archive": ("Archives left after extracting", True), "release": ("Release clutter (.txt, .url, .exe…)", True),
    "subs": ("Subtitle files with no matching video", False), "extras": ("Extras (trailers, featurettes…)", False),
    "empty": ("Folders with no video left in them", False),
}
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".webm", ".flv"}

def junk_kind(name, dirpath, has_video, size, mtime):
    low, ext = name.lower(), os.path.splitext(name)[1].lower()
    if low.startswith(TMP_PREFIX):
        return "partial" if time.time() - mtime > 6 * 3600 else None
    if ext in VIDEO_EXT:
        if (re.search(r"(^|[\W_])sample([\W_]|$)", name, re.I) or Path(dirpath).name.lower() in ("sample", "samples")) and size < 500e6:
            return "sample"
        if EXTRA_SUFFIX.search(os.path.splitext(name)[0]) or Path(dirpath).name.lower() in EXTRA_DIRS:
            return "extras"
        return None
    if ext in (".part", ".partial", ".crdownload", ".!qb", ".!ut", ".tmp", ".downloading"):
        return "partial"
    if (ext in (".rar", ".zip", ".7z", ".par2", ".sfv", ".nzb") or re.fullmatch(r"\.r\d\d", ext)) and has_video:
        return "archive"
    if ext in (".txt", ".url", ".exe", ".lnk", ".html", ".htm", ".website", ".bat", ".com", ".scr", ".msi"):
        return "release"
    return None

def find_junk():
    try:
        out = []
        for lib in CFG["libraries"]:
            skip = {d.lower() for d in CFG["ignore_dirs"]}
            video_under = {}                          # folder -> has any video in its subtree
            walk = list(os.walk(lib["path"]))
            pruned = []
            for dirpath, dirs, files in reversed(walk):
                has = any(os.path.splitext(f)[1].lower() in VIDEO_EXT and not f.startswith(".") for f in files)
                video_under[dirpath] = has or any(video_under.get(os.path.join(dirpath, d), False) for d in dirs)
            for dirpath, dirs, files in walk:               # top-down, so a parent is always seen before its children
                if any(inside(dirpath, p) for p in pruned):
                    continue
                rel = Path(dirpath).relative_to(lib["path"]).parts
                if any(p.startswith((".", "@", "#")) or p.lower() in skip for p in rel) or is_protected(os.path.join(dirpath, "x")) \
                        or any(m in files for m in IGNORE_MARKERS):
                    pruned.append(dirpath)
                    continue
                JUNK["msg"] = f"looking in {Path(dirpath).name[:40]}…"
                if 0 < len(rel) <= 2 and not video_under.get(dirpath) and rel[-1].lower() not in ("subs", "subtitles", "sample", "samples"):
                    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(dirpath) for f in fs)
                    out.append({"path": dirpath, "cat": "empty", "size": size, "dir": True})
                    pruned.append(dirpath)
                    continue
                stems = [os.path.splitext(f)[0].lower() for f in files if os.path.splitext(f)[1].lower() in VIDEO_EXT]
                for f in files:
                    if f.startswith(".") and not f.startswith(TMP_PREFIX):
                        continue
                    p = os.path.join(dirpath, f)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    kind = junk_kind(f, dirpath, bool(stems) or video_under.get(str(Path(dirpath).parent), False), st.st_size, st.st_mtime)
                    if not kind and os.path.splitext(f)[1].lower() in (".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"):
                        base = f.lower()
                        if not any(base.startswith(s) for s in stems):
                            kind = "subs"
                    if kind:
                        out.append({"path": p, "cat": kind, "size": st.st_size, "dir": False})
        out.sort(key=lambda j: -j["size"])
        JUNK.update(items=out, msg=f"found {len(out)} items, {sum(j['size'] for j in out) / 1e9:.2f} GB")
    except Exception as e:
        JUNK["msg"] = f"failed: {e}"
    finally:
        JUNK["running"] = False


# ---------- media server sessions & transcode watcher ----------
def server_sessions(app):
    """What's playing on one server: [{file, device, title, what, session}]; what = parts being transcoded."""
    out = []
    if app == "plex":
        for m in plex_get("/status/sessions").get("Metadata", []):
            ts = m.get("TranscodeSession") or {}
            parts = [p for md in m.get("Media", []) for p in md.get("Part", [])]
            burn = any(st.get("decision") == "burn" for p in parts for st in p.get("Stream", []))
            what = [k for k in ("video", "audio") if ts.get(f"{k}Decision") == "transcode"]
            if burn or ts.get("subtitleDecision") == "burn":
                what.append("subtitles")
            player = m.get("Player") or {}
            out.append({"file": parts[0].get("file", "") if parts else "", "session": m.get("sessionKey"), "what": what,
                        "device": " ".join(x for x in (player.get("title"), f"({player.get('product') or player.get('platform') or ''})") if x and x != "()"),
                        "title": " - ".join(x for x in (m.get("grandparentTitle"), m.get("title")) if x)})
    elif app in ("jellyfin", "emby"):
        for se in jf_request(app, "/Sessions", ActiveWithinSeconds=120):
            npi = se.get("NowPlayingItem")
            if not npi:
                continue
            what = []
            if (se.get("PlayState") or {}).get("PlayMethod") == "Transcode":
                ti = se.get("TranscodingInfo") or {}
                what = [k for k, f in (("video", "IsVideoDirect"), ("audio", "IsAudioDirect")) if ti.get(f) is False]
                if any("subtitle" in r.lower() for r in ti.get("TranscodeReasons") or []):
                    what.append("subtitles")
            out.append({"file": npi.get("Path", ""), "session": se.get("Id"), "what": what,
                        "device": f"{se.get('DeviceName', '?')} ({se.get('Client', '')})".replace(" ()", ""),
                        "title": " - ".join(x for x in (npi.get("SeriesName"), npi.get("Name")) if x)})
    elif app == "kodi":                                 # Kodi plays files itself, so it never transcodes
        for p in kodi_rpc("Player.GetActivePlayers") or []:
            if p.get("type") == "video":
                it = kodi_rpc("Player.GetItem", {"playerid": p["playerid"], "properties": ["file", "title", "showtitle"]}).get("item", {})
                out.append({"file": it.get("file", ""), "session": p["playerid"], "what": [], "device": "Kodi",
                            "title": " - ".join(x for x in (it.get("showtitle"), it.get("title") or it.get("label")) if x)})
    return out

_COUNTED = set()

def poll_sessions():
    """Check every server: is anyone watching (for the pause), and is anything being transcoded (for Health)."""
    busy, where, changed = False, [], False
    for app in servers():
        try:
            sessions = server_sessions(app)
        except Exception:
            continue
        if sessions:
            busy = True
            where.append(SERVERS[app])
        for m in sessions:
            if not m["what"] or not m["file"]:
                continue
            d = match_dir(local_dir_index(), m["file"])
            local = os.path.join(d, m["file"].replace("\\", "/").split("/")[-1]) if d else ""
            key = f"{app}|{m['file']}|{m['device']}"
            with LOCK:
                r = TRANSCODES.setdefault(key, {"server": SERVERS[app], "file": m["file"], "local": local, "device": m["device"],
                                                "title": m["title"], "what": [], "count": 0, "first": time.strftime("%Y-%m-%d %H:%M")})
                r["what"] = sorted(set(r["what"]) | set(m["what"]))
                r["last"] = time.strftime("%Y-%m-%d %H:%M")
                sk = f"{m['session']}|{r['first']}"
                if (key, sk) not in _COUNTED and r.get("last_session") != m["session"]:
                    _COUNTED.add((key, sk))
                    r["count"] += 1
                r["last_session"] = m["session"]
                changed = True
    _SESS.update(busy=busy, where=" and ".join(where), t=time.time())
    if changed:
        with LOCK:
            save_json(TRANSCODES_PATH, TRANSCODES)

def watch_transcodes():
    """Every 30 s while Reclaimer runs."""
    while True:
        time.sleep(30)
        if not MISSING and servers():
            poll_sessions()

def transcode_list():
    out = []
    for key, r in TRANSCODES.items():
        info = CACHE.get(r["local"], {}).get("info") if r["local"] else None
        rec = None
        if info:
            planp = plan_for_path(r["local"])[1]
            rec = {"action": planp[0]["action"], "reasons": planp[0]["reasons"]} if planp else None
        out.append({**r, "key": key, "codec": info and info["codec"], "audio": info and ", ".join(f"{a['codec']} {a['ch']}ch" for a in info["audio"]),
                    "subs": info and ", ".join(s["codec"] for s in info["subs"]), "rec": rec})
    return sorted(out, key=lambda r: r.get("last", ""), reverse=True)


# ---------- progress history ----------
def history():
    rows = []
    if LOG_PATH.exists():
        with open(LOG_PATH, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r.get("status") in ("done", "removed")]
    days, libs, total = {}, {}, 0
    for r in rows:
        b = int(float(r.get("bytes_saved") or 0))
        day = (r.get("time") or "")[:10]
        days[day] = days.get(day, 0) + b
        lib = library_of(r["file"])
        name = lib["path"] if lib else "other"
        libs[name] = libs.get(name, 0) + b
        total += b
    cum, series = 0, []
    for day in sorted(days):
        cum += days[day]
        series.append({"day": day, "saved": days[day], "total": cum})
    recs = recommendations()
    remaining = sum(r["size"] - r["est"] for r in recs)
    dupes = sum(g["savings"] for g in find_duplicates())
    return {"series": series, "by_lib": sorted(({"lib": k, "saved": v} for k, v in libs.items()), key=lambda x: -x["saved"]),
            "total": total, "remaining": remaining, "dupes": dupes, "junk": sum(j["size"] for j in JUNK["items"]),
            "held": originals_summary()["total"]}


# ---------- system tray ----------
STARTUP_FILE = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Reclaimer.vbs"

def status_text():
    q = QUEUE
    if MISSING:
        return "Needs ffmpeg - open to set up"
    if SCAN["running"]:
        return f"Scanning {SCAN['done']}/{SCAN['total']}"
    if q["waiting"]:
        return q["waiting"][0].upper() + q["waiting"][1:]
    if q["current"]:
        item = next((i for i in q["items"] if i["path"] == q["current"]), {})
        left = sum(1 for i in q["items"] if i["status"] == "queued")
        return f"Encoding {Path(q['current']).name[:40]} {item.get('progress', 0):.0f}% ({left} queued)"
    return f"Idle - {gb_text(q['saved'])} saved this session" if q["saved"] else "Idle"

def gb_text(b):
    return f"{b / 1e12:.2f} TB" if b >= 1e12 else f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"

ICON_COLORS = {"idle": "#5aa9ff", "working": "#4fc38a", "paused": "#e3a84f"}
ICON_INK = "#0b1a2e"

def icon_state():
    """idle (blue), working (green: encoding) or paused (amber: waiting for the window or a viewer)."""
    if QUEUE.get("current"):
        return "working"
    return "paused" if QUEUE.get("waiting") else "idle"

def icon_svg(state="idle"):
    """The Reclaimer icon: a disk with a slice freed up and a plus where it was. Drawn on a 64x64 grid."""
    c = ICON_COLORS.get(state, ICON_COLORS["idle"])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="2" y="2" width="60" height="60" rx="14" fill="{c}"/>'
            f'<path d="M26 38 L26 19 A19 19 0 1 0 45 38 Z" fill="{ICON_INK}"/>'
            f'<rect x="33" y="17.5" width="20" height="7" rx="2.3" fill="{ICON_INK}"/>'
            f'<rect x="39.5" y="11" width="7" height="20" rx="2.3" fill="{ICON_INK}"/></svg>')

def tray_image(state="idle", size=64):
    """Same icon as a PIL image (drawn 4x larger, then scaled down for smooth edges)."""
    from PIL import Image, ImageDraw
    k = 4 * size / 64
    sc = lambda *v: tuple(x * k for x in v)
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(sc(2, 2, 62, 62), radius=14 * k, fill=ICON_COLORS.get(state, ICON_COLORS["idle"]))
    d.pieslice(sc(26 - 19, 38 - 19, 26 + 19, 38 + 19), 0, 270, fill=ICON_INK)     # three quarters; top-right is freed
    d.rounded_rectangle(sc(33, 17.5, 53, 24.5), radius=2.3 * k, fill=ICON_INK)    # the plus where the slice was
    d.rounded_rectangle(sc(39.5, 11, 46.5, 31), radius=2.3 * k, fill=ICON_INK)
    return img.resize((size, size), Image.LANCZOS)

def write_ico(path):
    """Icon file for the .exe build."""
    tray_image("idle", 256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

def toggle_notify():
    with LOCK:
        CFG["notify"] = not CFG["notify"]
        save_json(CONFIG_PATH, CFG)

def startup_enabled():
    return STARTUP_FILE.exists()

def toggle_startup():
    if startup_enabled():
        STARTUP_FILE.unlink()
        return
    target = [sys.executable] if getattr(sys, "frozen", False) else \
        [str(Path(sys.executable).with_name("pythonw.exe")) if Path(sys.executable).with_name("pythonw.exe").exists() else sys.executable,
         str(Path(__file__).resolve())]
    line = " ".join(f'""{t}""' for t in target) + " --no-browser"
    STARTUP_FILE.write_text(f'CreateObject("WScript.Shell").Run "{line}", 0\n', encoding="utf-8")

def run_tray(url, srv):
    """Show the tray icon (blocks). Returns False if pystray isn't available."""
    try:
        import pystray
    except Exception:
        return False
    def quit_app(icon, _):
        if _PROC["p"]:
            _PROC["cancel"] = True
            _PROC["p"].kill()
        icon.stop()
        srv.shutdown()
    def queue_toggle(icon, _):
        if QUEUE["running"]:
            QUEUE["stop"] = True
        else:
            start_queue()
    menu = pystray.Menu(
        pystray.MenuItem("Open Reclaimer", lambda icon, _: webbrowser.open(url), default=True),
        pystray.MenuItem(lambda _: status_text(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: "Stop after current file" if QUEUE["running"] else "Start queue", queue_toggle,
                         enabled=lambda _: QUEUE["running"] or any(i["status"] == "queued" for i in QUEUE["items"])),
        pystray.MenuItem("Notifications", lambda icon, _: toggle_notify(), checked=lambda _: CFG["notify"]),
        pystray.MenuItem("Start with Windows", lambda icon, _: toggle_startup(), checked=lambda _: startup_enabled(),
                         visible=os.name == "nt"),
        pystray.MenuItem(lambda _: "Quit (cancels the current encode)" if QUEUE["current"] else "Quit", quit_app))
    icon = pystray.Icon("Reclaimer", tray_image(), "Reclaimer", menu)
    TRAY["icon"] = icon

    def watch(icon):
        icon.visible = True
        was_running, shown = False, "idle"
        while True:
            time.sleep(3)
            icon.title = ("Reclaimer - " + status_text())[:127]
            state = icon_state()
            if state != shown:
                icon.icon, shown = tray_image(state), state
            if was_running and not QUEUE["running"] and CFG["notify"]:
                done = sum(1 for i in QUEUE["items"] if i["status"] == "done")
                failed = sum(1 for i in QUEUE["items"] if i["status"] in ("failed", "rejected"))
                try:
                    icon.notify(f"{done} files done, {gb_text(QUEUE['saved'])} saved" + (f", {failed} need a look" if failed else ""), "Reclaimer finished")
                except Exception:
                    pass
            was_running = QUEUE["running"]
    icon.run(setup=lambda i: threading.Thread(target=watch, args=(i,), daemon=True).start())
    return True

def main():
    global CFG, CACHE, STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None, help=f"default: the Port setting ({DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true", help="run in the terminal instead of the system tray")
    args = ap.parse_args()
    if sys.stdout is None:   # started with pythonw (no console): keep messages in a log file instead
        sys.stdout = sys.stderr = open(HERE / "reclaimer_run.log", "a", encoding="utf-8", buffering=1)
    CFG = json.loads(json.dumps(DEFAULT_CONFIG))
    CFG.update(load_json(CONFIG_PATH, {}))
    for name, p in CFG["presets"].items():          # presets saved before descriptions existed
        if not p.get("desc") and name in PRESET_DESCRIPTIONS:
            p["desc"] = PRESET_DESCRIPTIONS[name]
    for name, p in DEFAULT_PRESETS.items():
        CFG["presets"].setdefault(name, p)
    if CFG.get("rules_version", 1) < 2:              # rules saved before the everyday-genres rule existed
        if not any("Drama" in r.get("genre", "") for r in CFG["plex_rules"]):
            CFG["plex_rules"].append(dict(GENERAL_RULE))
        CFG["rules_version"] = 2
    save_json(CONFIG_PATH, CFG)
    CACHE, STATE = load_cache(), load_json(STATE_PATH, {})
    GENRES.update(load_json(GENRES_PATH, {}))
    ARR_MAP.update(load_json(ARR_PATH, {}))
    TRANSCODES.update(load_json(TRANSCODES_PATH, {}))
    GUESS.update(load_json(GUESS_PATH, {}))
    if check_tools():
        init_engine()
    else:
        print(f"{' and '.join(MISSING)} not found - the page in your browser explains how to get it.")
    srv, port = open_server(args.port or int(CFG.get("port") or DEFAULT_PORT), args.no_browser)
    threading.Thread(target=auto_loop, daemon=True).start()
    threading.Thread(target=watch_transcodes, daemon=True).start()
    url = f"http://localhost:{port}"
    if not args.no_browser:
        webbrowser.open(url)
    if not args.no_tray:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Reclaimer running at {url} (in the system tray)")
        if run_tray(url, srv):
            return
        print("No tray icon available (run start.bat, or: pip install pystray pillow) - keep this window open.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            if _PROC["p"]:
                _PROC["cancel"] = True
                _PROC["p"].kill()
            return
    print(f"Reclaimer running at {url}  (Ctrl+C to quit)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if _PROC["p"]:
            _PROC["cancel"] = True
            _PROC["p"].kill()
        print("\nbye")

if __name__ == "__main__":
    main()
