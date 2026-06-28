# PlexMatch - Documentary Renamer

Scans your documentary library, matches files against TMDB, and renames/restructures them into Plex-compliant naming.

---

## Requirements

- Python 3.10+ (https://python.org)
- Node.js 18+ (https://nodejs.org)
- A free TMDB API key (https://www.themoviedb.org/settings/api)

---

## First-time setup

Open a terminal in this folder and run:

```
cd backend
pip install flask flask-cors requests

cd ../frontend
npm install
```

---

## Running the app

**Windows:** Double-click `start.bat`

**Mac/Linux:**

Terminal 1:
```
cd backend
python app.py
```

Terminal 2:
```
cd frontend
npm run dev
```

Then open http://localhost:5173 in your browser.

---

## How it works

1. **Configure** - Enter your TMDB API key and the path to your documentaries folder (e.g. `K:\Documentaries`)
2. **Scan** - The tool walks every subfolder, parses filenames, and matches each file against TMDB Movies and TMDB TV. Progress streams live so large libraries show a running count instead of a blank wait
3. **Review** - A table shows every file's current name, proposed Plex name, confidence score, and TMDB link. For each row you can:
   - Approve or reject the proposed rename
   - Expand the row to pick an alternative TMDB match (the proposed path is rebuilt automatically)
   - Type a corrected title (and optional year) and re-search manually when the automatic match is wrong or missing
4. **Rename** - Click Rename to move and restructure approved files into Plex-compliant folders. Optionally clean up source folders that are left empty
5. **Undo** - After a rename, one click reverts every moved file back to its original location

---

## Plex naming output

> **Important:** Plex needs movies and TV in *separate* libraries. A single library mixing both will ignore whichever type doesn't match its agent. With **Separate Movies and TV Shows** enabled (default), output is split into `Movies/` and `TV/` so you can point one Movie-agent library at `Movies/` and one TV-agent library at `TV/`.

**Films** (point a *Movie* library here):
```
Documentaries/
  Movies/
    Fukushima A Nuclear Nightmare (2026) {tmdb-1234567}/
      Fukushima A Nuclear Nightmare (2026).mkv
```

**TV documentary series** (point a *TV Show* library here):
```
Documentaries/
  TV/
    Making a Murderer (2015) {tmdb-61664}/
      Season 01/
        Making a Murderer (2015) - S01E01 - Plea of Innocence.mkv
```

The `{tmdb-…}` folder tag is added when **Add TMDB/IMDb IDs to folder names** is on, forcing Plex to match the exact title. Both toggles can be turned off to write a single flat structure without ID tags.

---

## Notes

- Settings (API key, folder path, preferences) are saved automatically between sessions in `backend/settings.json`
- All renames are shown as a dry run in the Review step before anything is touched
- The tool never deletes files - it only moves them. "Clean up empty folders" only removes directories that are already empty after a move
- Every rename batch is logged to `backend/rename_log.json` so it can be undone
- If a file has no TMDB match, it is flagged as unmatched - use the per-row manual search to correct it
- With "Clean names without a TMDB match" enabled, unmatched files still get a best-effort Plex name built from the folder (title/year) and filename (season/episode/episode title). These rows are badged **Local** and are not auto-approved - review them before renaming
- An optional **OMDb** key (free, IMDb-backed) is used as a fallback when TMDB has no confident match - it adds coverage for obscure docs, fills the `{imdb-...}` folder tag, and can supply episode names. Get a free key at [omdbapi.com](https://www.omdbapi.com/apikey.aspx)
- "Look up unmatched titles on Wikipedia" (no API key needed) refines those names with a canonical title, year, and episode name when a Wikipedia article exists; such rows are badged **Web** with a Wikipedia link. Titles with no Wikipedia article fall back to the **Local** filename cleanup
- Files already in the correct Plex location are flagged **organised** (hidden by default); a file whose target name is already taken is flagged a **conflict** so the rename never silently fails
- Rows missing a year are flagged; expand the row and use **Use as-is** to apply a typed title/year (no TMDB needed) - handy for docs not in any database, or just to add a year for better Plex matching
- Confidence scores: High (85%+), Medium (60-84%), Low (below 60%)

---

## API endpoints

| Method | Path                  | Purpose                                                        |
|--------|-----------------------|----------------------------------------------------------------|
| GET    | `/api/settings`       | Load persisted settings                                        |
| POST   | `/api/settings`       | Save settings                                                  |
| POST   | `/api/scan`           | Blocking scan (returns all proposals at once)                  |
| POST   | `/api/scan/stream`    | Streaming scan via Server-Sent Events (one proposal at a time) |
| POST   | `/api/rematch`        | Re-match one file by TMDB id (alternative) or manual title     |
| POST   | `/api/rename`         | Execute approved moves; logs the batch, optional folder cleanup|
| GET    | `/api/rename-log`     | List logged rename batches (most recent first)                 |
| POST   | `/api/undo`           | Reverse a rename batch (defaults to the most recent)           |
| POST   | `/api/pick-folder`    | Open the native OS folder picker and return the chosen path    |
| POST   | `/api/orphan-folders` | Preview folders under the root that contain no video files     |
| POST   | `/api/delete-folders` | Permanently delete the given (video-free) folders              |
| POST   | `/api/delete-files`   | Permanently delete specific files (e.g. duplicate sources)     |
| POST   | `/api/validate-folder`| Check whether a path exists on disk                            |
