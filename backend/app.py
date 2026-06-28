"""
Plex Documentary Renamer - Flask Backend
----------------------------------------
Scans a folder, matches files against TMDB (movies + TV),
proposes Plex-compliant renames, and executes approved ones.
"""

import os
import re
import sys
import json
import shutil
import requests
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# How many files to match against TMDB at once during a scan. TMDB lookups are
# network-bound, so concurrency dramatically reduces total scan time.
SCAN_CONCURRENCY = 8

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
        "clean_unmatched": True,
        "web_lookup": True,
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
        bbc|itv|pbs|nhk|natgeo|mvgroup|             # doc networks / release sites
        yify|yts|rarbg|ettv|eztv|               # groups (common)
        [a-z0-9]+-[a-z0-9]+$                        # release group at end
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")

# Episode markers, in priority order. SxxExx is most explicit; "N of M" and
# "Part N" / "Episode N" are common in documentary rips (e.g. "2of7").
SXXEXX_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
NOFM_PATTERN = re.compile(r"\b(\d{1,2})\s*of\s*(\d{1,2})\b", re.IGNORECASE)
PART_PATTERN = re.compile(r"\b(?:part|pt|episode|ep)\s*\.?\s*(\d{1,2})\b", re.IGNORECASE)

# Folder names that carry no useful title info, so they must never be used as a
# search hint (e.g. a single episode inside a "Season 01" folder).
SEASON_FOLDER_PATTERN = re.compile(r"^(season[\s._-]*\d+|specials|s\d{1,2})$", re.IGNORECASE)

# "sample" as a whole token in a filename stem -> a throwaway preview clip
SAMPLE_PATTERN = re.compile(r"(?:^|[\s._-])sample(?:[\s._-]|$)", re.IGNORECASE)

# An embedded IMDb rating tag, e.g. "IMDB 8.2" / "imdb8.1"
IMDB_RATING_PATTERN = re.compile(r"\bimdb\b\s*\d+(?:\.\d+)?", re.IGNORECASE)


def find_episode_marker(name: str):
    """Return (season, episode, start, end) for the first episode marker, else None."""
    m = SXXEXX_PATTERN.search(name)
    if m:
        return int(m.group(1)), int(m.group(2)), m.start(), m.end()
    m = NOFM_PATTERN.search(name)
    if m:
        return 1, int(m.group(1)), m.start(), m.end()
    m = PART_PATTERN.search(name)
    if m:
        return 1, int(m.group(1)), m.start(), m.end()
    return None


def clean_episode_title(text: str) -> str:
    """Tidy the text that follows an episode marker into a usable episode name."""
    text = IMDB_RATING_PATTERN.sub("", text)
    text = re.sub(r"\(?\b(19[5-9]\d|20[0-3]\d)\b\)?", "", text)  # stray year
    text = NOISE_PATTERN.sub("", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s\-\[\]\(\)]+|[\s\-\[\]\(\);,]+$", "", text).strip()
    return text


def parse_filename(raw: str) -> dict:
    """
    Extract title, year, season, episode from a raw filename/folder name.
    Returns a dict with keys: title, year, season, episode, is_tv
    """
    name = raw
    # Strip file extension if present
    name = re.sub(r"\.[a-z0-9]{2,4}$", "", name, flags=re.IGNORECASE)

    # Detect episode info, trying the most explicit form first.
    season = episode = None
    is_tv = False
    marker_start = None

    m = SXXEXX_PATTERN.search(name)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        is_tv = True
        marker_start = m.start()
    else:
        m = NOFM_PATTERN.search(name)  # e.g. "2of7", "2 of 7"
        if m:
            season, episode = 1, int(m.group(1))
            is_tv = True
            marker_start = m.start()
        else:
            m = PART_PATTERN.search(name)  # e.g. "Part 2", "Episode 2"
            if m:
                season, episode = 1, int(m.group(1))
                is_tv = True
                marker_start = m.start()

    # Everything from the episode marker onward is series-noise / episode title;
    # the canonical episode name comes from TMDB.
    if marker_start is not None:
        name = name[:marker_start]

    # Extract year
    year_match = YEAR_PATTERN.search(name)
    year = int(year_match.group(0)) if year_match else None

    # Remove everything from the year (or resolution/source markers) onward
    if year_match:
        name = name[: year_match.start()]

    # Strip an embedded IMDb rating tag, then remaining noise
    name = IMDB_RATING_PATTERN.sub("", name)
    name = NOISE_PATTERN.sub("", name)

    # Clean separators: dots, underscores, multiple spaces → single space
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Remove trailing/leading hyphens and brackets
    name = re.sub(r"^[\s\-\[\]\(\)]+|[\s\-\[\]\(\)]+$", "", name).strip()

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

# Cached so repeated titles (e.g. many episodes of one series) aren't looked up
# twice. lru_cache is thread-safe, which matters for the concurrent scan.
@lru_cache(maxsize=2048)
def tmdb_search_movie(title: str, year: int | None, api_key: str) -> tuple:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["year"] = year
    r = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=10)
    if r.status_code != 200:
        return ()
    return tuple(r.json().get("results", []))


@lru_cache(maxsize=2048)
def tmdb_search_tv(title: str, year: int | None, api_key: str) -> tuple:
    params = {"api_key": api_key, "query": title, "include_adult": False}
    if year:
        params["first_air_date_year"] = year
    r = requests.get(f"{TMDB_BASE}/search/tv", params=params, timeout=10)
    if r.status_code != 200:
        return ()
    return tuple(r.json().get("results", []))


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
# Wikipedia fallback (free, no API key) - used when TMDB has no confident match
# ---------------------------------------------------------------------------

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "PlexMatch/1.0 (local documentary renamer)"}


@lru_cache(maxsize=512)
def wiki_search(query: str) -> tuple:
    """Return ((page_title, snippet), ...) candidates for a query, best first."""
    try:
        r = requests.get(WIKI_API, params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 5, "srnamespace": 0, "format": "json",
        }, headers=WIKI_HEADERS, timeout=10)
        if r.status_code != 200:
            return ()
        hits = r.json().get("query", {}).get("search", [])
        return tuple((h["title"], h.get("snippet", "")) for h in hits)
    except requests.RequestException:
        return ()


@lru_cache(maxsize=512)
def wiki_wikitext(page_title: str) -> str:
    """Fetch the raw wikitext of a page (following redirects)."""
    try:
        r = requests.get(WIKI_API, params={
            "action": "parse", "page": page_title, "prop": "wikitext",
            "redirects": 1, "format": "json",
        }, headers=WIKI_HEADERS, timeout=10)
        if r.status_code != 200:
            return ""
        return r.json().get("parse", {}).get("wikitext", {}).get("*", "")
    except requests.RequestException:
        return ""


def wiki_canonical_title(page_title: str) -> str:
    """Drop a trailing disambiguation qualifier, e.g. 'Cosmos (1980 TV series)'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", page_title).strip()


def _clean_wiki_markup(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", s)  # [[A|B]]->B, [[A]]->A
    s = re.sub(r"\{\{.*?\}\}", "", s)                          # drop templates
    s = s.replace("''", "").replace('"', "").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def wiki_year(wikitext: str) -> int | None:
    m = re.search(r"\{\{\s*[Ss]tart date\s*\|\s*(\d{4})", wikitext)
    if m:
        return int(m.group(1))
    m = re.search(
        r"\|\s*(?:first_aired|released|air_date|first_run|date)\s*=\s*[^\n]*?(19[5-9]\d|20[0-3]\d)",
        wikitext,
    )
    if m:
        return int(m.group(1))
    return None


def wiki_episode_title(wikitext: str, episode: int) -> str | None:
    """Best-effort: pull a title from {{Episode list}} entries by episode number."""
    for chunk in re.split(r"\{\{\s*[Ee]pisode list", wikitext)[1:]:
        chunk = chunk[:1500]  # an entry is short; avoid bleeding into the next
        num = re.search(r"EpisodeNumber\s*=\s*([0-9]+)", chunk)
        title = re.search(r"\bTitle\s*=\s*([^\n|]+)", chunk)
        if num and title and int(num.group(1)) == episode:
            cleaned = _clean_wiki_markup(title.group(1))
            if cleaned:
                return cleaned
    return None


def wiki_lookup(title: str, year: int | None, season, episode) -> dict | None:
    """Find a confident Wikipedia page for `title`; return canonical title/year
    (and episode name when possible). None if nothing matches well enough."""
    from difflib import SequenceMatcher

    results = wiki_search(title)
    if not results:
        return None

    best_page, best_score = None, 0.0
    for page_title, _snippet in results:
        cand = wiki_canonical_title(page_title)
        score = SequenceMatcher(None, title.lower(), cand.lower()).ratio()
        if score > best_score:
            best_score, best_page = score, page_title

    if not best_page or best_score < 0.6:
        return None

    wikitext = wiki_wikitext(best_page)
    ep_title = wiki_episode_title(wikitext, episode) if (season and episode) else None

    return {
        "title": wiki_canonical_title(best_page),
        "year": wiki_year(wikitext) or year,
        "episode_name": ep_title,
        "wiki_url": "https://en.wikipedia.org/wiki/" + best_page.replace(" ", "_"),
        "confidence": round(best_score, 3),
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

    # Plex prefers "Title (Year)" but a year isn't always known (local cleanups)
    title_year = f"{title} ({year})" if year else title

    if media_type == "movie":
        folder = title_year
        filename = f"{title_year}{original_ext}"
    else:
        season = match["season"] or 1
        episode = match["episode"] or 1
        ep_title = sanitise_filename(match["episode_name"]) if match["episode_name"] else ""
        season_folder = f"Season {season:02d}"
        folder = os.path.join(title_year, season_folder)
        if ep_title:
            filename = f"{title_year} - S{season:02d}E{episode:02d} - {ep_title}{original_ext}"
        else:
            filename = f"{title_year} - S{season:02d}E{episode:02d}{original_ext}"

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

        # Skip subtitle/extras/sample folders
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in {"subs", "subtitles", "extras", "featurettes", "behind the scenes", "sample", "samples"}
        ]

        # Drop obvious sample clips (e.g. "...-sample.avi", "movie.sample.mkv")
        video_files = [f for f in video_files if not SAMPLE_PATTERN.search(Path(f).stem)]

        for vf in video_files:
            full_path = os.path.join(dirpath, vf)
            rel_dir = os.path.relpath(dirpath, root)

            # If this is the only video file in its folder, the folder name is
            # usually a cleaner hint than a cryptic release filename - EXCEPT for
            # "Season NN"/"Specials" folders, whose names carry no title.
            folder_name = Path(dirpath).name
            use_folder_as_hint = (
                len(video_files) == 1
                and folder_name.lower() not in {"documentaries", os.path.basename(root).lower()}
                and not SEASON_FOLDER_PATTERN.match(folder_name.strip())
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

def proposal_status(proposed_full_path: str, current_full_path: str, default: str = "pending") -> str:
    """organised if already in place, conflict if target taken, else `default`."""
    def _norm(p):
        return os.path.normcase(os.path.normpath(p))
    if _norm(proposed_full_path) == _norm(current_full_path):
        return "organised"
    if os.path.exists(proposed_full_path):
        return "conflict"
    return default


def series_folder_name(full_path: str, root: str) -> str | None:
    """The title-bearing folder for a file (its parent, or grandparent if the
    parent is a 'Season NN' folder). None for files sitting directly in root."""
    parent = os.path.dirname(full_path)
    if os.path.abspath(parent) == os.path.abspath(root):
        return None
    name = os.path.basename(parent)
    if SEASON_FOLDER_PATTERN.match(name.strip()):
        grand = os.path.dirname(parent)
        if os.path.abspath(grand) != os.path.abspath(root):
            return os.path.basename(grand)
        return None
    return name


def build_local_cleanup(f: dict, root: str, web_lookup: bool = False) -> dict | None:
    """
    Best-effort Plex naming WITHOUT TMDB: derive the title/year from the series
    folder and the season/episode/episode-title from the filename. When
    web_lookup is set, refine the title/year/episode-name via Wikipedia.
    Returns a proposal dict (status cleanup/organised/conflict) or None.
    """
    filename_stem = Path(f["filename"]).stem

    folder_name = series_folder_name(f["full_path"], root)
    folder_parsed = parse_filename(folder_name) if folder_name else {"title": "", "year": None}
    file_parsed = parse_filename(filename_stem)

    # Season/episode + episode title come from the filename's marker
    marker = find_episode_marker(filename_stem)
    season = episode = None
    episode_title = ""
    if marker:
        season, episode, _start, end = marker
        episode_title = clean_episode_title(filename_stem[end:])
    else:
        season, episode = file_parsed["season"], file_parsed["episode"]

    # Title: a folder named "Title (Year)" is the most reliable source; without a
    # year the folder is often just a release name, so prefer the filename's title.
    folder_title, file_title = folder_parsed["title"], file_parsed["title"]
    if folder_parsed["year"] or not file_title:
        title = folder_title or file_title
    else:
        title = file_title or folder_title
    if not title:
        return None
    year = folder_parsed["year"] or file_parsed["year"]
    is_tv = bool(season and episode)

    # Drop a redundant series-name prefix from the episode title
    if episode_title and episode_title.lower().startswith(title.lower()):
        episode_title = re.sub(r"^[\s\-:;,]+", "", episode_title[len(title):]).strip()

    # Optionally refine via Wikipedia: canonical title, year, episode name
    source = "local"
    info_url = None
    confidence = 0
    if web_lookup:
        hit = wiki_lookup(title, year, season, episode)
        if hit:
            source = "wikipedia"
            title = hit["title"]
            year = hit["year"]
            if hit["episode_name"]:
                episode_title = hit["episode_name"]
            info_url = hit["wiki_url"]
            confidence = hit["confidence"]

    media_type = "tv" if is_tv else "movie"
    match_like = {
        "matched_title": title,
        "matched_year": str(year) if year else "",
        "type": media_type,
        "season": season,
        "episode": episode,
        "episode_name": episode_title or None,
    }
    plex_names = build_plex_names(match_like, f["ext"])
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])
    status = proposal_status(proposed_full_path, f["full_path"], default="cleanup")

    return {
        **f,
        "parsed": file_parsed,
        "matched": False,
        "confidence": confidence,
        "type": media_type,
        "matched_title": title,
        "matched_year": match_like["matched_year"],
        "episode_name": episode_title or None,
        "season": season,
        "episode": episode,
        "local_cleanup": True,
        "source": source,
        "wiki_url": info_url,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "status": status,
    }


def build_proposal(f: dict, root: str, api_key: str,
                   clean_unmatched: bool = False, web_lookup: bool = False) -> dict:
    """Match a single scanned file against TMDB and build a proposal dict."""
    do_cleanup = clean_unmatched or web_lookup
    parsed = parse_filename(f["parse_hint"])
    if not parsed["title"]:
        if do_cleanup:
            cleanup = build_local_cleanup(f, root, web_lookup=web_lookup)
            if cleanup:
                return cleanup
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
        if do_cleanup:
            cleanup = build_local_cleanup(f, root, web_lookup=web_lookup)
            if cleanup:
                return cleanup
        return {**f, "parsed": parsed, **match, "status": "unmatched"}

    plex_names = build_plex_names(match, f["ext"])
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])
    status = proposal_status(proposed_full_path, f["full_path"], default="pending")

    return {
        **f,
        "parsed": parsed,
        **match,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "status": status,
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


def _subtree_has_video(dirpath: str) -> bool:
    """True if dirpath or any descendant contains a video file."""
    for _dp, _dn, filenames in os.walk(dirpath):
        if any(Path(f).suffix.lower() in VIDEO_EXTENSIONS for f in filenames):
            return True
    return False


def find_orphan_folders(root: str) -> list:
    """
    Find the top-most folders under root whose entire subtree contains no video
    files (e.g. leftover release folders holding only .txt/.nfo/.jpg). A folder
    is reported only if its parent DOES still contain video (or is root), so a
    junk folder rolls up to a single deletable entry instead of every subfolder.
    """
    root_abs = os.path.abspath(root)

    # Bottom-up pass: does each directory's subtree contain any video?
    has_video = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dp_abs = os.path.abspath(dirpath)
        vid_here = any(Path(f).suffix.lower() in VIDEO_EXTENSIONS for f in filenames)
        vid_child = any(has_video.get(os.path.abspath(os.path.join(dirpath, d)), False) for d in dirnames)
        has_video[dp_abs] = vid_here or vid_child

    orphans = []
    for dirpath in has_video:
        if dirpath == root_abs or has_video[dirpath]:
            continue
        parent = os.path.dirname(dirpath)
        # Report only the top-most empty folder (parent still has video, or is root)
        if parent != root_abs and not has_video.get(parent, True):
            continue

        # Summarise what's inside so the user knows what they're deleting
        file_count = 0
        exts = set()
        for _dp, _dn, filenames in os.walk(dirpath):
            file_count += len(filenames)
            for f in filenames:
                suffix = Path(f).suffix.lower()
                if suffix:
                    exts.add(suffix)

        orphans.append({
            "path": dirpath,
            "rel_path": os.path.relpath(dirpath, root),
            "name": os.path.basename(dirpath),
            "file_count": file_count,
            "extensions": sorted(exts),
        })

    orphans.sort(key=lambda o: o["rel_path"].lower())
    return orphans


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

    clean_unmatched = bool(data.get("clean_unmatched", False))
    web_lookup = bool(data.get("web_lookup", False))
    files = scan_directory(root)
    with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as executor:
        # executor.map preserves input order
        proposals = list(executor.map(
            lambda f: build_proposal(f, root, api_key, clean_unmatched, web_lookup), files
        ))
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

    clean_unmatched = bool(data.get("clean_unmatched", False))
    web_lookup = bool(data.get("web_lookup", False))
    files = scan_directory(root)

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        yield sse({"type": "start", "total": len(files), "root": root})
        # Match files concurrently (TMDB calls are network-bound) and stream
        # each proposal as soon as it's ready.
        with ThreadPoolExecutor(max_workers=SCAN_CONCURRENCY) as executor:
            futures = {
                executor.submit(build_proposal, f, root, api_key, clean_unmatched, web_lookup): i
                for i, f in enumerate(files)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    proposal = future.result()
                except Exception as e:  # defensive: never break the stream
                    proposal = {
                        **files[i], "matched": False, "confidence": 0,
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
    proposed_full_path = os.path.join(root, plex_names["folder"], plex_names["filename"])

    def _norm(p):
        return os.path.normcase(os.path.normpath(p))
    full_path = data.get("full_path", "")
    if full_path and _norm(proposed_full_path) == _norm(full_path):
        status = "organised"
    elif os.path.exists(proposed_full_path):
        status = "conflict"
    else:
        status = "pending"

    return jsonify({
        **match,
        "parsed": parsed,
        "proposed_folder": plex_names["folder"],
        "proposed_filename": plex_names["filename"],
        "proposed_full_path": proposed_full_path,
        "status": status,
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


# Runs in a separate process so the GUI event loop never touches the Flask
# server thread (tkinter is not thread-safe). Prints the chosen path (or an
# empty line if cancelled) to stdout.
_PICKER_CODE = r"""
import sys
import tkinter as tk
from tkinter import filedialog

initial = sys.argv[1] if len(sys.argv) > 1 else ""
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
kwargs = {"title": "Select documentaries folder"}
import os
if initial and os.path.isdir(initial):
    kwargs["initialdir"] = initial
path = filedialog.askdirectory(**kwargs)
root.destroy()
sys.stdout.write(path or "")
"""


@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Open the native OS folder-picker dialog and return the chosen path."""
    data = request.json or {}
    initial = (data.get("initial") or "").strip()

    run_kwargs = {"capture_output": True, "text": True, "timeout": 600}
    if os.name == "nt":
        # Avoid a console window flashing when spawning python.exe
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            [sys.executable, "-c", _PICKER_CODE, initial],
            **run_kwargs,
        )
    except Exception as e:
        return jsonify({"error": f"Could not open folder dialog: {e}"}), 500

    path = (result.stdout or "").strip()

    if result.returncode != 0 and not path:
        return jsonify({
            "error": (result.stderr or "").strip() or "Folder dialog failed to open",
        }), 500

    if path:
        path = os.path.normpath(path)  # askdirectory returns forward slashes on Windows

    return jsonify({"path": path, "cancelled": path == ""})


@app.route("/api/validate-folder", methods=["POST"])
def validate_folder():
    path = request.json.get("path", "")
    exists = os.path.isdir(path)
    return jsonify({"exists": exists, "path": path})


@app.route("/api/orphan-folders", methods=["POST"])
def orphan_folders():
    """Preview folders under root that contain no video files (safe to delete)."""
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400
    return jsonify({"folders": find_orphan_folders(root)})


@app.route("/api/delete-folders", methods=["POST"])
def delete_folders():
    """
    Delete the given folders. Each is re-verified to be inside root and to
    contain no video files before removal, so an out-of-date preview can never
    delete a folder that has since gained a video.
    Body: { root_folder, folders: [<abs path>, ...] }
    """
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    folders = data.get("folders") or []

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400

    root_abs = os.path.abspath(root)
    results = []
    for folder in folders:
        target = os.path.abspath(folder)
        try:
            # Safety: must live strictly inside root and never be root itself
            if target == root_abs or os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("Folder is outside the library root")
            if not os.path.isdir(target):
                raise FileNotFoundError("Folder no longer exists")
            if _subtree_has_video(target):
                raise ValueError("Folder now contains a video file - skipped")

            shutil.rmtree(target)
            results.append({"path": folder, "ok": True})
        except Exception as e:  # noqa: BLE001 - report any failure per-folder
            results.append({"path": folder, "ok": False, "error": str(e)})

    return jsonify({"results": results})


@app.route("/api/delete-files", methods=["POST"])
def delete_files():
    """
    Delete individual files (used to remove duplicate source files whose target
    is already organised). Each file is re-verified to live inside root.
    Body: { root_folder, files: [<abs path>, ...] }
    """
    data = request.json or {}
    root = (data.get("root_folder") or "").strip()
    files = data.get("files") or []

    if not root or not os.path.isdir(root):
        return jsonify({"error": f"Folder not found: {root}"}), 400

    root_abs = os.path.abspath(root)
    results = []
    for fpath in files:
        target = os.path.abspath(fpath)
        try:
            if target == root_abs or os.path.commonpath([root_abs, target]) != root_abs:
                raise ValueError("File is outside the library root")
            if not os.path.isfile(target):
                raise FileNotFoundError("File no longer exists")
            os.remove(target)
            results.append({"path": fpath, "ok": True})
        except Exception as e:  # noqa: BLE001 - report any failure per-file
            results.append({"path": fpath, "ok": False, "error": str(e)})

    return jsonify({"results": results})


if __name__ == "__main__":
    # threaded=True so the streaming scan endpoint doesn't block other requests
    app.run(port=5174, debug=True, threaded=True)
