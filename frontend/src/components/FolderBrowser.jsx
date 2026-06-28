import { useState, useEffect, useCallback } from "react"

const API = "http://localhost:5174"

export default function FolderBrowser({ initialPath, onSelect, onClose }) {
  const [path, setPath] = useState(initialPath || "")
  const [parent, setParent] = useState(null)
  const [folders, setFolders] = useState([])
  const [drives, setDrives] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const browse = useCallback(async (target) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/browse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: target ?? "" }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not open that folder")
      setPath(data.path)
      setParent(data.parent)
      setFolders(data.folders)
      setDrives(data.drives)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    browse(initialPath || "")
  }, [browse, initialPath])

  const join = (name) => {
    if (!path) return name
    const sep = path.includes("\\") ? "\\" : "/"
    return path.endsWith(sep) ? `${path}${name}` : `${path}${sep}${name}`
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="folder-browser" onClick={e => e.stopPropagation()}>
        <div className="fb-header">
          <span className="fb-title">Choose documentaries folder</span>
          <button className="fb-close" onClick={onClose} title="Close">✕</button>
        </div>

        <div className="fb-toolbar">
          <button
            className="btn-ghost"
            onClick={() => browse(parent ?? "")}
            disabled={loading || parent === null}
            title="Up one level"
          >
            ↑ Up
          </button>
          <span className="fb-path" title={path || "This PC"}>
            {path || "This PC"}
          </span>
        </div>

        {drives.length > 0 && (
          <div className="fb-drives">
            {drives.map(d => (
              <button
                key={d}
                className={`fb-drive ${path.toUpperCase().startsWith(d.toUpperCase()) ? "active" : ""}`}
                onClick={() => browse(d)}
                disabled={loading}
              >
                {d}
              </button>
            ))}
          </div>
        )}

        <div className="fb-list">
          {loading && <div className="fb-empty"><div className="spinner-sm" /> Loading...</div>}
          {!loading && error && <div className="fb-error">{error}</div>}
          {!loading && !error && folders.length === 0 && (
            <div className="fb-empty">
              {path ? "No subfolders here." : "Pick a drive above to start browsing."}
            </div>
          )}
          {!loading && !error && folders.map(name => (
            <button key={name} className="fb-folder" onClick={() => browse(join(name))} disabled={loading}>
              <svg viewBox="0 0 16 16" fill="none">
                <path d="M2 4.5A1.5 1.5 0 013.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0114 6v6a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 12V4.5z" stroke="currentColor" strokeWidth="1.2"/>
              </svg>
              <span>{name}</span>
            </button>
          ))}
        </div>

        <div className="fb-footer">
          <span className="fb-selected">{path || "No folder selected"}</span>
          <div className="fb-actions">
            <button className="btn-secondary" onClick={onClose}>Cancel</button>
            <button
              className="btn-primary"
              onClick={() => onSelect(path)}
              disabled={!path || loading}
            >
              Select this folder
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
