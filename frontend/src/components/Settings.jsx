import { useState, useEffect } from "react"
import OrphanCleanup from "./OrphanCleanup"

const API = "http://localhost:5174"

export default function Settings({ settings, onChange, onScan, scanning, stage }) {
  const [folderValid, setFolderValid] = useState(null)
  const [validating, setValidating] = useState(false)
  const [localFolder, setLocalFolder] = useState(settings.root_folder || "")
  const [picking, setPicking] = useState(false)
  const [pickError, setPickError] = useState(null)
  const [cleaningOrphans, setCleaningOrphans] = useState(false)

  // Keep the input in sync when the stored folder changes (load, self-heal, pick)
  useEffect(() => {
    setLocalFolder(settings.root_folder || "")
  }, [settings.root_folder])

  const update = (key, value) => {
    onChange({ ...settings, [key]: value })
  }

  const validateFolder = async (path) => {
    if (!path) return
    setValidating(true)
    try {
      const res = await fetch(`${API}/api/validate-folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      })
      const data = await res.json()
      setFolderValid(data.exists)
      if (data.exists) update("root_folder", path)
    } catch {
      setFolderValid(false)
    } finally {
      setValidating(false)
    }
  }

  const pickFolder = async () => {
    setPicking(true)
    setPickError(null)
    try {
      const res = await fetch(`${API}/api/pick-folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initial: localFolder }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not open folder picker")
      if (data.cancelled || !data.path) return // user cancelled the dialog
      setLocalFolder(data.path)
      setFolderValid(true)
      update("root_folder", data.path)
    } catch (e) {
      setPickError(e.message)
    } finally {
      setPicking(false)
    }
  }

  const canScan = settings.tmdb_api_key && settings.root_folder && folderValid !== false

  return (
    <div className="settings">
      <div className="settings-section">
        <h3 className="settings-heading">API</h3>
        <label className="field-label">TMDB API Key</label>
        <input
          type="password"
          className="field-input"
          placeholder="Enter your TMDB v3 key"
          value={settings.tmdb_api_key || ""}
          onChange={e => update("tmdb_api_key", e.target.value)}
        />
        <a
          className="field-hint-link"
          href="https://www.themoviedb.org/settings/api"
          target="_blank"
          rel="noreferrer"
        >
          Get a free key at themoviedb.org →
        </a>
      </div>

      <div className="settings-section">
        <h3 className="settings-heading">Library</h3>
        <label className="field-label">Documentaries folder</label>
        <div className="folder-input-row">
          <input
            type="text"
            className={`field-input ${folderValid === false ? "field-input--error" : folderValid === true ? "field-input--ok" : ""}`}
            placeholder="Click Browse to choose a folder"
            value={localFolder}
            onChange={e => {
              setLocalFolder(e.target.value)
              setFolderValid(null)
            }}
            onBlur={e => validateFolder(e.target.value)}
          />
          {validating && <div className="spinner-sm" />}
          {!validating && folderValid === true && (
            <svg className="field-check" viewBox="0 0 16 16" fill="none">
              <path d="M3 8l4 4 6-6" stroke="#4ADE80" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          )}
          {!validating && folderValid === false && (
            <svg className="field-cross" viewBox="0 0 16 16" fill="none">
              <path d="M4 4l8 8M12 4l-8 8" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          )}
        </div>
        <button className="btn-secondary btn-browse" onClick={pickFolder} disabled={picking}>
          {picking ? (
            <>
              <div className="spinner-sm" />
              Waiting for dialog...
            </>
          ) : (
            <>
              <svg viewBox="0 0 16 16" fill="none">
                <path d="M2 4.5A1.5 1.5 0 013.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0114 6v6a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 12V4.5z" stroke="currentColor" strokeWidth="1.2"/>
              </svg>
              Browse...
            </>
          )}
        </button>
        {pickError && <span className="field-error">{pickError}</span>}
        {folderValid === false && (
          <span className="field-error">Folder not found. Check the path is accessible from this machine.</span>
        )}
        {folderValid === null && !pickError && (
          <span className="field-hint">Type a path, or click Browse to pick a folder</span>
        )}
      </div>

      <div className="settings-section">
        <h3 className="settings-heading">Options</h3>

        <label className="toggle-row">
          <div className="toggle-info">
            <span className="toggle-label">Auto-pick top match</span>
            <span className="toggle-sub">Always use the highest-confidence TMDB result</span>
          </div>
          <button
            role="switch"
            aria-checked={settings.auto_pick_top}
            className={`toggle ${settings.auto_pick_top ? "toggle--on" : ""}`}
            onClick={() => update("auto_pick_top", !settings.auto_pick_top)}
          />
        </label>

        <label className="toggle-row">
          <div className="toggle-info">
            <span className="toggle-label">Restructure into Plex folders</span>
            <span className="toggle-sub">Move files into <code>Title (Year)/</code> subfolders</span>
          </div>
          <button
            role="switch"
            aria-checked={settings.restructure_folders}
            className={`toggle ${settings.restructure_folders ? "toggle--on" : ""}`}
            onClick={() => update("restructure_folders", !settings.restructure_folders)}
          />
        </label>

        <label className="toggle-row">
          <div className="toggle-info">
            <span className="toggle-label">Clean up empty folders</span>
            <span className="toggle-sub">Remove source folders left empty after files move out</span>
          </div>
          <button
            role="switch"
            aria-checked={settings.clean_empty_folders}
            className={`toggle ${settings.clean_empty_folders ? "toggle--on" : ""}`}
            onClick={() => update("clean_empty_folders", !settings.clean_empty_folders)}
          />
        </label>

        <button
          className="btn-secondary btn-cleanup"
          onClick={() => setCleaningOrphans(true)}
          disabled={!settings.root_folder || folderValid === false}
          title="Find and delete folders that contain no video files"
        >
          <svg viewBox="0 0 16 16" fill="none">
            <path d="M3 4h10M6 4V3a1 1 0 011-1h2a1 1 0 011 1v1M5 4l.5 9a1 1 0 001 1h3a1 1 0 001-1L11 4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
          </svg>
          Clean up orphaned folders
        </button>
        <span className="toggle-sub">Delete folders with no video files (only <code>.txt</code>, <code>.nfo</code>, images, etc.)</span>
      </div>

      <button
        className="btn-primary btn-scan"
        onClick={() => onScan(settings.root_folder)}
        disabled={scanning || !canScan}
      >
        {scanning ? (
          <>
            <div className="spinner-sm" />
            Scanning...
          </>
        ) : (
          <>
            <svg viewBox="0 0 16 16" fill="none">
              <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5"/>
              <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
            Scan Library
          </>
        )}
      </button>

      {!settings.tmdb_api_key && (
        <p className="settings-warn">Enter a TMDB API key to scan.</p>
      )}

      {cleaningOrphans && (
        <OrphanCleanup root={settings.root_folder} onClose={() => setCleaningOrphans(false)} />
      )}
    </div>
  )
}
