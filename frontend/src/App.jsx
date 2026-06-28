import { useState, useEffect, useCallback } from "react"
import Settings from "./components/Settings"
import ScanPanel from "./components/ScanPanel"
import ProposalTable from "./components/ProposalTable"
import StatusBar from "./components/StatusBar"
import "./styles.css"

const API = "http://localhost:5174"

const STAGES = ["Configure", "Scan", "Review", "Rename"]

export default function App() {
  const [stage, setStage] = useState(0) // 0=configure 1=scan 2=review 3=done
  const [settings, setSettings] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [scanProgress, setScanProgress] = useState({ current: 0, total: 0 })
  const [proposals, setProposals] = useState([])
  const [renaming, setRenaming] = useState(false)
  const [renameResults, setRenameResults] = useState([])
  const [error, setError] = useState(null)
  const [scanRoot, setScanRoot] = useState("")
  const [lastBatch, setLastBatch] = useState(null)
  const [undoing, setUndoing] = useState(false)
  const [resultNote, setResultNote] = useState(null)

  // Load persisted settings on mount
  useEffect(() => {
    fetch(`${API}/api/settings`)
      .then(r => r.json())
      .then(s => setSettings(s))
      .catch(() => setSettings({
        tmdb_api_key: "",
        root_folder: "K:\\Documentaries",
        auto_pick_top: true,
        restructure_folders: true,
      }))
  }, [])

  const saveSettings = useCallback(async (updated) => {
    setSettings(updated)
    await fetch(`${API}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updated),
    })
  }, [])

  const handleScan = useCallback(async (rootOverride) => {
    setError(null)
    setProposals([])
    setRenameResults([])
    setResultNote(null)
    setScanProgress({ current: 0, total: 0 })
    setScanning(true)
    setStage(1)

    const root = rootOverride || settings.root_folder

    try {
      const res = await fetch(`${API}/api/scan/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: root,
          tmdb_api_key: settings.tmdb_api_key,
        }),
      })

      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || "Scan failed")
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE frames are separated by a blank line
        const frames = buffer.split("\n\n")
        buffer = frames.pop()

        for (const frame of frames) {
          const line = frame.replace(/^data:\s?/, "").trim()
          if (!line) continue
          const msg = JSON.parse(line)

          if (msg.type === "start") {
            setScanRoot(msg.root)
            setScanProgress({ current: 0, total: msg.total })
          } else if (msg.type === "proposal") {
            const p = {
              ...msg.proposal,
              approved: msg.proposal.status === "pending",
              selectedAlt: null,
            }
            setProposals(prev => [...prev, p])
            setScanProgress(prev => ({ ...prev, current: prev.current + 1 }))
          } else if (msg.type === "error") {
            throw new Error(msg.error)
          }
        }
      }

      setStage(2)
    } catch (e) {
      setError(e.message)
      setStage(0)
    } finally {
      setScanning(false)
    }
  }, [settings])

  const handleRename = useCallback(async () => {
    const approved = proposals
      .filter(p => p.approved && p.status === "pending")
      .map(p => ({
        full_path: p.full_path,
        proposed_full_path: p.proposed_full_path,
      }))

    if (!approved.length) return
    setRenaming(true)
    setResultNote(null)

    try {
      const res = await fetch(`${API}/api/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: scanRoot,
          approved,
          clean_empty_folders: settings.clean_empty_folders,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Rename failed")
      setRenameResults(data.results)
      setLastBatch(data.batch_id || null)
      if (data.removed_folders?.length) {
        setResultNote(`Cleaned up ${data.removed_folders.length} empty folder${data.removed_folders.length !== 1 ? "s" : ""}.`)
      }
      setStage(3)
    } catch (e) {
      setError(e.message)
    } finally {
      setRenaming(false)
    }
  }, [proposals, scanRoot, settings])

  const handleUndo = useCallback(async () => {
    if (!lastBatch) return
    setUndoing(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: lastBatch }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Undo failed")
      setRenameResults(data.results)
      setLastBatch(null)
      const ok = data.results.filter(r => r.ok).length
      setResultNote(`Reverted ${ok} file${ok !== 1 ? "s" : ""} to original location${ok !== 1 ? "s" : ""}.`)
    } catch (e) {
      setError(e.message)
    } finally {
      setUndoing(false)
    }
  }, [lastBatch])

  const approvedCount = proposals.filter(p => p.approved && p.status === "pending").length
  const matchedCount = proposals.filter(p => p.matched).length
  const unmatchedCount = proposals.filter(p => !p.matched).length

  if (!settings) {
    return (
      <div className="app-loading">
        <div className="spinner" />
        <span>Connecting to backend...</span>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-brand">
          <svg className="brand-icon" viewBox="0 0 24 24" fill="none">
            <path d="M12 2L2 7v10l10 5 10-5V7L12 2z" stroke="#F5A623" strokeWidth="1.5" fill="none"/>
            <path d="M12 2v15M2 7l10 5 10-5" stroke="#F5A623" strokeWidth="1.5"/>
          </svg>
          <span className="brand-name">PlexMatch</span>
          <span className="brand-sub">Documentary Renamer</span>
        </div>
        <div className="pipeline">
          {STAGES.map((s, i) => (
            <div key={s} className={`pipeline-stage ${i === stage ? "active" : ""} ${i < stage ? "done" : ""}`}>
              <div className="pipeline-dot">
                {i < stage ? (
                  <svg viewBox="0 0 12 12" fill="none">
                    <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                ) : (
                  <span>{i + 1}</span>
                )}
              </div>
              <span>{s}</span>
              {i < STAGES.length - 1 && <div className="pipeline-line" />}
            </div>
          ))}
        </div>
      </header>

      <div className="app-body">
        <aside className="sidebar">
          <Settings
            settings={settings}
            onChange={saveSettings}
            onScan={handleScan}
            scanning={scanning}
            stage={stage}
          />
        </aside>

        <main className="main-content">
          {error && (
            <div className="error-banner">
              <svg viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="7" stroke="#FF5F5F" strokeWidth="1.5"/>
                <path d="M8 4.5v4M8 10.5v1" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
              <span>{error}</span>
              <button onClick={() => setError(null)}>✕</button>
            </div>
          )}

          {stage === 0 && (
            <div className="empty-state">
              <div className="empty-icon">
                <svg viewBox="0 0 64 64" fill="none">
                  <rect x="8" y="16" width="48" height="36" rx="3" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M8 24h48" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M20 32h24M20 38h16" stroke="#3A3D42" strokeWidth="2" strokeLinecap="round"/>
                  <circle cx="48" cy="48" r="12" fill="#1A1D21" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M44 48l3 3 5-5" stroke="#F5A623" strokeWidth="2" strokeLinecap="round"/>
                </svg>
              </div>
              <h2>Configure and scan your library</h2>
              <p>Enter your TMDB API key and documentaries folder path in the panel on the left, then click <strong>Scan Library</strong>.</p>
            </div>
          )}

          {scanning && (
            <div className="scanning-state">
              {scanProgress.total > 0 ? (
                <>
                  <div className="scan-progress">
                    <div
                      className="scan-progress-fill"
                      style={{ width: `${Math.round((scanProgress.current / scanProgress.total) * 100)}%` }}
                    />
                  </div>
                  <p>Matching against TMDB...</p>
                  <p className="scan-sub">
                    {scanProgress.current} of {scanProgress.total} files processed
                  </p>
                </>
              ) : (
                <>
                  <div className="scan-animation">
                    <div className="scan-bar" />
                  </div>
                  <p>Discovering video files...</p>
                  <p className="scan-sub">This may take a moment for large libraries.</p>
                </>
              )}
            </div>
          )}

          {!scanning && stage === 2 && proposals.length > 0 && (
            <ProposalTable
              proposals={proposals}
              onChange={setProposals}
              root={scanRoot}
              apiKey={settings.tmdb_api_key}
            />
          )}

          {!scanning && stage === 2 && proposals.length === 0 && (
            <div className="empty-state">
              <h2>No video files found</h2>
              <p>Nothing to rename in <strong>{scanRoot}</strong>. Check the folder path and try again.</p>
              <button className="btn-secondary" onClick={() => setStage(0)}>← Back to settings</button>
            </div>
          )}

          {stage === 3 && (
            <div className="results-panel">
              <h2 className="results-heading">
                {lastBatch ? "Rename complete" : "Done"}
                <span className={`results-badge ${renameResults.filter(r => r.ok).length === renameResults.length ? "success" : "partial"}`}>
                  {renameResults.filter(r => r.ok).length} / {renameResults.length} succeeded
                </span>
              </h2>
              {resultNote && <p className="result-note">{resultNote}</p>}
              <div className="results-list">
                {renameResults.map((r, i) => (
                  <div key={i} className={`result-row ${r.ok ? "ok" : "fail"}`}>
                    <div className="result-icon">
                      {r.ok
                        ? <svg viewBox="0 0 12 12" fill="none"><path d="M2 6l3 3 5-5" stroke="#4ADE80" strokeWidth="1.5" strokeLinecap="round"/></svg>
                        : <svg viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/></svg>
                      }
                    </div>
                    <div className="result-paths">
                      <span className="result-src">{r.src}</span>
                      <span className="result-arrow">→</span>
                      <span className="result-dst">{r.ok ? r.dst : r.error}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="results-actions">
                <button className="btn-primary" onClick={() => { setStage(0); setProposals([]); setRenameResults([]); setResultNote(null) }}>
                  Scan another folder
                </button>
                {lastBatch && (
                  <button className="btn-secondary" onClick={handleUndo} disabled={undoing}>
                    {undoing ? "Undoing..." : "Undo this rename"}
                  </button>
                )}
              </div>
            </div>
          )}
        </main>
      </div>

      {stage === 2 && !scanning && proposals.length > 0 && (
        <footer className="app-footer">
          <div className="footer-stats">
            <span><strong>{proposals.length}</strong> files found</span>
            <span className="stat-dot" />
            <span><strong className="text-green">{matchedCount}</strong> matched</span>
            <span className="stat-dot" />
            <span><strong className="text-amber">{unmatchedCount}</strong> unmatched</span>
            <span className="stat-dot" />
            <span><strong>{approvedCount}</strong> approved for rename</span>
          </div>
          <div className="footer-actions">
            <button className="btn-secondary" onClick={() => setStage(0)}>
              ← Back
            </button>
            <button
              className="btn-primary"
              onClick={handleRename}
              disabled={renaming || approvedCount === 0}
            >
              {renaming ? "Renaming..." : `Rename ${approvedCount} file${approvedCount !== 1 ? "s" : ""}`}
            </button>
          </div>
        </footer>
      )}
    </div>
  )
}
