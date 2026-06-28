import { useState, useEffect, useCallback } from "react"

const API = "http://localhost:5174"

export default function OrphanCleanup({ root, onClose }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [folders, setFolders] = useState([]) // [{ path, rel_path, file_count, extensions, selected }]
  const [deleting, setDeleting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [results, setResults] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setResults(null)
    setConfirming(false)
    try {
      const res = await fetch(`${API}/api/orphan-folders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not scan for orphaned folders")
      setFolders(data.folders.map(f => ({ ...f, selected: true })))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [root])

  useEffect(() => { load() }, [load])

  const toggle = (i) =>
    setFolders(prev => prev.map((f, idx) => idx === i ? { ...f, selected: !f.selected } : f))
  const setAll = (val) =>
    setFolders(prev => prev.map(f => ({ ...f, selected: val })))

  const selected = folders.filter(f => f.selected)

  const doDelete = async () => {
    setDeleting(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/delete-folders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root, folders: selected.map(f => f.path) }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Delete failed")
      setResults(data.results)
      const deleted = new Set(data.results.filter(r => r.ok).map(r => r.path))
      setFolders(prev => prev.filter(f => !deleted.has(f.path)))
      setConfirming(false)
    } catch (e) {
      setError(e.message)
    } finally {
      setDeleting(false)
    }
  }

  const okCount = results?.filter(r => r.ok).length ?? 0
  const failCount = results ? results.length - okCount : 0

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal orphan-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Clean up orphaned folders</h3>
          <button className="modal-close" onClick={onClose} title="Close">✕</button>
        </div>

        <p className="modal-sub">
          Folders under your library that contain <strong>no video files</strong> -
          typically leftovers holding only <code>.txt</code>, <code>.nfo</code> or images.
          Deleting a folder removes everything inside it permanently.
        </p>

        {error && <div className="modal-error">{error}</div>}

        {results && (
          <div className="modal-results">
            Deleted {okCount} folder{okCount !== 1 ? "s" : ""}.
            {failCount > 0 && ` ${failCount} could not be removed.`}
          </div>
        )}

        <div className="orphan-body">
          {loading ? (
            <div className="orphan-loading"><div className="spinner-sm" /> Scanning library...</div>
          ) : folders.length === 0 ? (
            <div className="orphan-empty">No orphaned folders found. Your library is clean.</div>
          ) : (
            <>
              <div className="orphan-toolbar">
                <span>{selected.length} of {folders.length} selected</span>
                <div>
                  <button className="btn-ghost" onClick={() => setAll(true)}>Select all</button>
                  <button className="btn-ghost" onClick={() => setAll(false)}>Select none</button>
                </div>
              </div>
              <div className="orphan-list">
                {folders.map((f, i) => (
                  <label key={f.path} className="orphan-item">
                    <input type="checkbox" checked={f.selected} onChange={() => toggle(i)} />
                    <span className="orphan-name">{f.rel_path}</span>
                    <span className="orphan-meta">
                      {f.file_count} file{f.file_count !== 1 ? "s" : ""}
                      {f.extensions.length > 0 && ` (${f.extensions.join(", ")})`}
                    </span>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>Close</button>
          {folders.length > 0 && !confirming && (
            <button
              className="btn-danger"
              onClick={() => setConfirming(true)}
              disabled={selected.length === 0}
            >
              Delete {selected.length} folder{selected.length !== 1 ? "s" : ""}
            </button>
          )}
          {confirming && (
            <div className="confirm-inline">
              <span>Permanently delete {selected.length} folder{selected.length !== 1 ? "s" : ""}?</span>
              <button className="btn-ghost" onClick={() => setConfirming(false)} disabled={deleting}>Cancel</button>
              <button className="btn-danger" onClick={doDelete} disabled={deleting}>
                {deleting ? "Deleting..." : "Yes, delete"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
