"""
Plex Documentary Renamer - Flask Backend
----------------------------------------
Scans a folder, matches files against TMDB (movies + TV),
proposes Plex-compliant renames, and executes approved ones.
"""

import os
import re
import json
import shutil
import requests
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")
RENAME_LOG_FILE = os.path.join(os.path.dirname(__file__), "rename_log.json")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".flv", ".webm"
}

TMDB_BASE = "https://api.themoviedb.org/3"

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {
        "tmdb_api_key": "",
        "root_folder": "",
        "auto_pick_top": True,
        "restructure_folders": True,
        "clean_empty_folders": False,
    }

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.json
    save_settings(data)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Common noise tokens to strip when building a clean search title
NOISE_PATTERN = re.compile(
    r"""
    \b(
        4k|2160p|1080p|1080i|720p|480p|576p|       # resolution
        bluray|blu-ray|bdrip|bdrip|bray|            # source
        webrip|web-rip|web-dl|webdl|amzn|nf|hulu|  # web sources
        hbo|dsnp|atvp|pcok|                         # streaming
        hdtv|pdtv|dvdrip|dvdscr|dvd|               # other sources
        x264|x265|h264|h\.264|h265|h\.265|          # codecs
        xvid|divx|hevc|avc|                         # codecs
        dd5\.1|dd2\.0|aac|ac3|dts|truehd|atmos|    # audio
        dts-hd|dts\.hd|                             # audio
        proper|repack|extended|theatrical|           # release flags
        directors\.cut|unrated|remastered|           # release flags
        yify|yts|rarbg|ettv|eztv|               # groups (common)
        [a-z0-9]+-[a-z0-9]+$                        # release group at end
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
EPISODE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")


def parse_filename(raw: str) -> dict:
    """
    Extract title, year, season, episode from a raw filename/folder name.
    Returns a dict with keys: title, year, season, episode, is_tv
    """
    name = raw
    # Strip file extension if present
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.IGNORECASE)

    # Detect episode info
    ep_match = EPISODE_PATTERN.search(name)
    season = int(ep_match.group(1)) if ep_match else None
    episode = int(ep_match.group(2)) if ep_match else None
    is_tv = ep_match is not None

    # Remove episode pattern before further parsing
    if ep_match:
        name = name[: ep_match.start()]

    # Extract year
    year_match = YEAR_PATTERN.search(name)
    year = int(year_match.group(0)) if year_match else None

    # Remove everything from the year (or resolution/source markers) onward
    if year_match:
        name = name[: year_match.start()]

    # Strip remaining noise
    name = NOISE_PATTERN.sub("", name)

    # Clean separators: dots, underscores, multiple spaces → single space
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Remove trailing/leading hyphens and brackets
    name = re.sub(r"^[\s\-\[\(]+|[\s\-\[\)]+$", "", name).strip()

    return {
        "title": name,
        "year": year,
        "season": season,
        "episode": episode,
        "is_tv": is_tv,
    }


# ---------------------------------------------------------------------------
# TMDB lookups
# ---------------------------------------------------------------------------

def tmdb_search_movie(title: str, year: int | None, api_key: str) -> list:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["year"] = year
    r = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=10)
    if r.status_code != 200:
        return []
    return r.json().get("results", [])


def tmdb_search_tv(title: str, year: int | None, api_key: str) -> list:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["first_air_date_year"] = year
    r = requests.get(f"{TMDB_BASE}/search/tv", params=params, timeout=10)
    if r.status_code != 200:
        return []
    return r.json().get("results", [])


def tmdb_episode_name(tv_id: int, season: int, episode: int, api_key: str) -> str | None:
    r = requests.get(
        f"{TMDB_BASE}/tv/{tv_id}/season/{season}/episode/{episode}",
        params={"api_key": api_key},
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("name")
    return None


def tmdb_get_details(media_type: str, tmdb_id: int, api_key: str) -> dict | None:
    """Fetch a single movie/TV record by id (used for manual re-selection)."""
    path = "movie" if media_type == "movie" else "tv"
    r = requests.get(
        f"{TMDB_BASE}/{path}/{tmdb_id}",
        params={"api_key": api_key},
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json()


def build_match_from_details(details: dict, media_type: str, parsed: dict, api_key: str,
                             confidence: float = 1.0) -> dict:
    """Build a match dict from a specific TMDB record (a user-chosen title)."""
    matched_title = details.get("title") or details.get("name") or ""
    raw_date = details.get("release_date") or details.get("first_air_date") or ""
    matched_year = raw_date[:4] if raw_date else str(parsed.get("year") or "")
    tmdb_id = details.get("id")

    episode_name = None
    if media_type == "tv" and parsed.get("season") and parsed.get("episode"):
        episode_name = tmdb_episode_name(
            tmdb_id, parsed["season"], parsed["episode"], api_key
        )

    return {
        "matched": True,
        "confidence": confidence,
        "type": media_type,
        "tmdb_id": tmdb_id,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "episode_name": episode_name,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "tmdb_url": f"https://www.themoviedb.org/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}",
    }


def score_result(result: dict, parsed_title: str, parsed_year: int | None, is_tv: bool) -> float:
    """
    Simple confidence score 0-1 based on title similarity and year match.
    """
    from difflib import SequenceMatcher

    candidate_title = result.get("title") or result.get("name") or ""
    candidate_year_str = (
        result.get("release_date") or result.get("first_air_date") or ""
    )[:4]
    candidate_year = int(candidate_year_str) if candidate_year_str.isdigit() else None

    title_score = SequenceMatcher(
        None, parsed_title.lower(), candidate_title.lower()
    ).ratio()

    year_score = 1.0
    if parsed_year and candidate_year:
        diff = abs(parsed_year - candidate_year)
        year_score = 1.0 if diff == 0 else (0.7 if diff == 1 else 0.3)
    elif parsed_year and not candidate_year:
        year_score = 0.8

    return round(title_score * 0.7 + year_score * 0.3, 3)


def find_best_match(parsed: dict, api_key: str) -> dict:
    """
    Search TMDB (movies + TV). Return best match with confidence score.
    """
    title = parsed["title"]
    year = parsed["year"]
    is_tv = parsed["is_tv"]

    candidates = []

    # Always search both unless clearly TV (has S/E pattern)
    if not is_tv:
        for r in tmdb_search_movie(title, year, api_key):
            candidates.append({"result": r, "type": "movie"})

    for r in tmdb_search_tv(title, year, api_key):
        candidates.append({"result": r, "type": "tv"})

    # If TV-patterned, also check movies (some docs use SxxExx loosely)
    if is_tv:
        for r in tmdb_search_movie(title, year, api_key):
            candidates.append({"result": r, "type": "movie"})

    if not candidates:
        return {"matched": False, "confidence": 0}

    scored = []
    for c in candidates:
        score = score_result(c["result"], title, year, is_tv)
        scored.append({**c, "confidence": score})

    scored.sort(key=lambda x: x["confidence"], reverse=True)
    best = scored[0]

    r = best["result"]
    media_type = best["type"]
    confidence = best["confidence"]

    matched_title = r.get("title") or r.get("name") or ""
    raw_date = r.get("release_date") or r.get("first_air_date") or ""
    matched_year = raw_date[:4] if raw_date else str(year or "")
    tmdb_id = r.get("id")

    # Fetch episode name if TV
    episode_name = None
    if media_type == "tv" and parsed["season"] and parsed["episode"]:
        episode_name = tmdb_episode_name(
            tmdb_id, parsed["season"], parsed["episode"], api_key
        )

    return {
        "matched": True,
        "confidence": confidence,
        "type": media_type,
        "tmdb_id": tmdb_id,
        "matched_title": matched_title,
        "matched_year": matched_year,
        "episode_name": episode_name,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "tmdb_url": f"https://www.themoviedb.org/{'tv' if media_type == 'tv' else 'movie'}/{tmdb_id}",
        "alternatives": [
            {
                "title": s["result"].get("title") or s["result"].get("name"),
                "year": (s["result"].get("release_date") or s["result"].get("first_air_date") or "")[:4],
                "type": s["type"],
                "tmdb_id": s["result"].get("id"),
                "confidence": s["confidence"],
            }
            for s in scored[1:4]  # up to 3 alternatives
        ],
    }


# ---------------------------------------------------------------------------
# Plex naming
# ---------------------------------------------------------------------------

def sanitise_filename(name: str) -> str:
    """Remove characters not safe for Windows/Mac/Linux filenames."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def build_plex_names(match: dict, original_ext: str) -> dict:
    """
    Returns proposed folder path (relative to root) and filename.
    """
    title = sanitise_filename(match["matched_title"])
    year = match["matched_year"]
    media_type = match["type"]

    if media_type == "movie":
        folder = f"{title} ({year})"
        filename = f"{title} ({year}){original_ext}"
    else:
        season = match["season"] or 1
        episode = match["episode"] or 1
        ep_title = sanitise_filename(match["episode_name"]) if match["episode_name"] else ""
        season_folder = f"Season {season:02d}"
        folder = os.path.join(f"{title} ({year})", season_folder)
        if ep_title:
            filename = f"{title} ({year}) - S{season:02d}E{episode:02d} - {ep_title}{original_ext}"
        else:
            filename = f"{title} ({year}) - S{season:02d}E{episode:02d}{original_ext}"

    return {"folder": folder, "filename": filename}


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def scan_directory(root: str) -> list:
    """
    Walk root recursively. For each video file found, return its path and
    the best display name to parse (prefers parent folder name for single-file
    folders, falls back to filename).
    """
    results = []
    root_path = Path(root)

    for dirpath, dirnames, filenames in os.walk(root):
        # Filter to video files only
        video_files = [
            f for f in filenames
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS
        ]

        # Skip subtitle/extras folders
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in {"subs", "subtitles", "extras", "featurettes", "behind the scenes"}
        ]

        for vf in video_files:
            full_path = os.path.join(dirpath, vf)
            rel_dir = os.path.relpath(dirpath, root)

            # If this is the only video file in its folder, use folder name as hint
            folder_name = Path(dirpath).name
            use_folder_as_hint = (
                len(video_files) == 1
                and folder_name.lower() not in {"documentaries", os.path.basename(root).lower()}
            )

            parse_hint = folder_name if use_folder_as_hint else Path(vf).stem

            results.append({
                "full_path": full_path,
                "rel_path": os.path.relpath(full_path, root),
                "filename": vf,
                "folder": rel_dir,
                "parse_hint": parse_hint,
                "ext": Path(vf).suffix.lower(),
            })

    return results


# ---------------------------------------------------------------------------
# Proposal building
# ---------------------------------------------------------------------------

def build_proposal(f: dict, root: str, api_key: str) -> dict:
    """Match a single scanned file against TMDB and build a proposal dict."""
    parsed = parse_filename(f["parse_hint"])
    if not parsed["title"]:
        return {
            **f,
            "parsed": parsed,
            "matched": False,
            "confidence": 0,
            "status": "unmatched",
            "error": "Could not parse a title from filename",
        }

    try:
        match = find_best_match(parsed, api_key)
    except Exception as e:
        return {
            **f,
            "parsed": parsed,
            "matched": False,
            "confidence": 0,
            "status": "error",
            "error": str(e),
        }

    if not match["matched"] or match["confidence"] < 0.3:
        return {**f, "parsed": parsed, **match, "status": "unmatched"}

    plex_names = build_plex_names(match, f["ext"])
    return {
        **f,
        "parsed": parsed,
        **match,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": os.path.join(root, plex_names["folder"], plex_names["filename"]),
        "status": "pending",  # pending | approved | rejected | done | error
    }


# ---------------------------------------------------------------------------
# Rename log + folder cleanup helpers
# ---------------------------------------------------------------------------

def load_rename_log() -> list:
    if os.path.exists(RENAME_LOG_FILE):
        try:
            with open(RENAME_LOG_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_rename_log(log: list) -> None:
    with open(RENAME_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


def log_rename_batch(moves: list, root: str) -> str:
    """Append a batch of successful moves and return its batch id."""
    log = load_rename_log()
    now = datetime.now()
    batch_id = now.strftime("%Y%m%d%H%M%S%f")
    log.append({
        "batch_id": batch_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "root": root,
        "moves": moves,  # [{ "src": original, "dst": new }]
        "undone": False,
    })
    save_rename_log(log)
    return batch_id


def remove_empty_dirs(root: str) -> list:
    """Remove empty directories under root (never root itself). Bottom-up."""
    removed = []
    root_abs = os.path.abspath(root)
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == root_abs:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed.append(dirpath)
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/scan", methods=["POST"])
def scan():
    """Blocking scan: returns all proposals at once (legacy/fallback)."""
    data = request.json
    root = data.get("root_folder", "").strip()
    api_key = data.get("tmdb_api_key", "").strip()

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    if not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400

    files = scan_directory(root)
    proposals = [build_proposal(f, root, api_key) for f in files]
    return jsonify({"proposals": proposals, "root": root})


@app.route("/api/scan/stream", methods=["POST"])
def scan_stream():
    """
    Streaming scan via Server-Sent Events. Emits one proposal at a time so the
    UI can show live progress on large libraries.
    Events (JSON in each `data:` frame):
      { "type": "start", "total": N, "root": ... }
      { "type": "proposal", "index": i, "proposal": {...} }
      { "type": "done" }
      { "type": "error", "error": ... }
    """
    data = request.json
    root = data.get("root_folder", "").strip()
    api_key = data.get("tmdb_api_key", "").strip()

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    if not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400

    files = scan_directory(root)

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        yield sse({"type": "start", "total": len(files), "root": root})
        for i, f in enumerate(files):
            try:
                proposal = build_proposal(f, root, api_key)
            except Exception as e:  # defensive: never break the stream
                proposal = {
                    **f, "matched": False, "confidence": 0,
                    "status": "error", "error": str(e),
                }
            yield sse({"type": "proposal", "index": i, "proposal": proposal})
        yield sse({"type": "done"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/rematch", methods=["POST"])
def rematch():
    """
    Re-match a single file, either by:
      - a specific TMDB id + media_type (chosen from alternatives), or
      - a manual_title (+ optional manual_year) free-text re-search.
    Returns refreshed proposal fields including a correctly-rooted
    proposed_full_path.
    """
    data = request.json or {}
    root = data.get("root_folder", "")
    api_key = data.get("tmdb_api_key", "").strip()
    ext = data.get("ext", "")
    parsed = data.get("parsed") or {}

    if not api_key:
        return jsonify({"error": "TMDB API key is required"}), 400

    tmdb_id = data.get("tmdb_id")
    media_type = data.get("media_type")
    manual_title = (data.get("manual_title") or "").strip()
    manual_year = data.get("manual_year")

    if tmdb_id and media_type:
        details = tmdb_get_details(media_type, tmdb_id, api_key)
        if not details:
            return jsonify({"error": "Could not load that TMDB title"}), 502
        confidence = data.get("confidence", 1.0)
        match = build_match_from_details(details, media_type, parsed, api_key, confidence)
    elif manual_title:
        year = None
        if manual_year is not None and str(manual_year).strip().isdigit():
            year = int(str(manual_year).strip())
        else:
            year = parsed.get("year")
        search_parsed = {
            "title": manual_title,
            "year": year,
            "season": parsed.get("season"),
            "episode": parsed.get("episode"),
            "is_tv": parsed.get("is_tv", False),
        }
        try:
            match = find_best_match(search_parsed, api_key)
        except Exception as e:
            return jsonify({"error": str(e)}), 502
        parsed = search_parsed
    else:
        return jsonify({"error": "Provide either a TMDB id or a manual title"}), 400

    if not match.get("matched"):
        return jsonify({"matched": False, "confidence": 0, "status": "unmatched"})

    plex_names = build_plex_names(match, ext)
    return jsonify({
        **match,
        "parsed": parsed,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": os.path.join(root, plex_names["folder"], plex_names["filename"]),
        "status": "pending",
    })


@app.route("/api/rename", methods=["POST"])
def rename():
    """
    Execute approved renames. Moves files into new folder structure, logs the
    batch for undo, and optionally cleans up empty source folders.
    """
    data = request.json
    root = data.get("root_folder", "")
    approved = data.get("approved", [])  # list of proposal objects
    clean_empty = bool(data.get("clean_empty_folders", False))

    results = []
    moves = []

    for item in approved:
        src = item["full_path"]
        dst = item["proposed_full_path"]

        try:
            if os.path.abspath(src) == os.path.abspath(dst):
                results.append({"src": src, "dst": dst, "ok": True})
                continue
            if os.path.exists(dst):
                raise FileExistsError(f"Target already exists: {dst}")
            dst_dir = os.path.dirname(dst)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(src, dst)
            results.append({"src": src, "dst": dst, "ok": True})
            moves.append({"src": src, "dst": dst})
        except Exception as e:
            results.append({"src": src, "dst": dst, "ok": False, "error": str(e)})

    removed_folders = []
    if clean_empty and root and os.path.isdir(root):
        removed_folders = remove_empty_dirs(root)

    batch_id = log_rename_batch(moves, root) if moves else None

    return jsonify({
        "results": results,
        "batch_id": batch_id,
        "removed_folders": removed_folders,
    })


@app.route("/api/rename-log", methods=["GET"])
def rename_log():
    """Return logged rename batches, most recent first."""
    log = load_rename_log()
    return jsonify({"batches": list(reversed(log))})


@app.route("/api/undo", methods=["POST"])
def undo():
    """
    Reverse a rename batch (moves files back to their original paths).
    Body: { "batch_id": ... }  -- if omitted, the most recent batch is undone.
    """
    data = request.json or {}
    batch_id = data.get("batch_id")
    log = load_rename_log()
    active = [b for b in log if not b.get("undone")]

    if not active:
        return jsonify({"error": "Nothing to undo"}), 400

    if batch_id:
        batch = next((b for b in log if b["batch_id"] == batch_id and not b.get("undone")), None)
    else:
        batch = active[-1]

    if not batch:
        return jsonify({"error": "Batch not found or already undone"}), 404

    results = []
    for mv in reversed(batch["moves"]):
        original, current = mv["src"], mv["dst"]
        try:
            if not os.path.exists(current):
                raise FileNotFoundError(f"File no longer at {current}")
            if os.path.exists(original):
                raise FileExistsError(f"Original path is occupied: {original}")
            os.makedirs(os.path.dirname(original), exist_ok=True)
            shutil.move(current, original)
            results.append({"src": current, "dst": original, "ok": True})
        except Exception as e:
            results.append({"src": current, "dst": original, "ok": False, "error": str(e)})

    removed_folders = []
    if batch.get("root") and os.path.isdir(batch["root"]):
        removed_folders = remove_empty_dirs(batch["root"])

    batch["undone"] = True
    save_rename_log(log)

    return jsonify({
        "results": results,
        "removed_folders": removed_folders,
        "batch_id": batch["batch_id"],
    })


@app.route("/api/validate-folder", methods=["POST"])
def validate_folder():
    path = request.json.get("path", "")
    exists = os.path.isdir(path)
    return jsonify({"exists": exists, "path": path})


if __name__ == "__main__":
    # threaded=True so the streaming scan endpoint doesn't block other requests
    app.run(port=5174, debug=True, threaded=True)
