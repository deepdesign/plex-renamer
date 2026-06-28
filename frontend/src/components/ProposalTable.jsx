import { useState, useMemo } from "react"

const API = "http://localhost:5174"

const CONFIDENCE_LABEL = (c) => {
  if (c >= 0.85) return { label: "High", cls: "conf-high" }
  if (c >= 0.6) return { label: "Medium", cls: "conf-med" }
  if (c > 0) return { label: "Low", cls: "conf-low" }
  return { label: "None", cls: "conf-none" }
}

function ConfidencePip({ value }) {
  const { label, cls } = CONFIDENCE_LABEL(value)
  const pct = Math.round(value * 100)
  return (
    <div className={`confidence ${cls}`}>
      <div className="conf-bar">
        <div className="conf-fill" style={{ width: `${pct}%` }} />
      </div>
      <span>{pct}% {label}</span>
    </div>
  )
}

function ProposalRow({ proposal, index, onChange, onRemove, root, apiKey }) {
  const [expanded, setExpanded] = useState(false)
  const [manualTitle, setManualTitle] = useState("")
  const [manualYear, setManualYear] = useState("")
  const [busy, setBusy] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const toggle = () => {
    onChange(index, { ...proposal, approved: !proposal.approved })
  }

  // Ask the backend to rebuild a proposal (correct root-prefixed full path,
  // episode-name lookup for TV, etc). Handles both alt selection and manual search.
  const rematch = async (payload) => {
    setBusy(true)
    setSearchError(null)
    try {
      const res = await fetch(`${API}/api/rematch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: root,
          tmdb_api_key: apiKey,
          full_path: proposal.full_path,
          rel_path: proposal.rel_path,
          filename: proposal.filename,
          folder: proposal.folder,
          ext: proposal.ext,
          parsed: proposal.parsed,
          ...payload,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Search failed")
      if (!data.matched) {
        setSearchError("No TMDB match found. Try a different title or add a year.")
        return
      }
      onChange(index, {
        ...proposal,
        ...data,
        // alt selection keeps the alternatives list; manual search replaces it
        selectedAlt: payload.tmdb_id ? { title: data.matched_title, year: data.matched_year, type: data.type } : null,
        approved: true,
      })
      setExpanded(false)
    } catch (e) {
      setSearchError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const selectAlt = (alt) =>
    rematch({ tmdb_id: alt.tmdb_id, media_type: alt.type, confidence: alt.confidence })

  const manualSearch = () => {
    if (!manualTitle.trim()) return
    rematch({ manual_title: manualTitle.trim(), manual_year: manualYear.trim() })
  }

  const deleteSource = async () => {
    setBusy(true)
    setSearchError(null)
    try {
      const res = await fetch(`${API}/api/delete-files`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root, files: [proposal.full_path] }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Delete failed")
      const r = data.results?.[0]
      if (!r?.ok) throw new Error(r?.error || "Could not delete file")
      onRemove(index)
    } catch (e) {
      setSearchError(e.message)
      setBusy(false)
    }
  }

  const isUnmatched = proposal.status === "unmatched"
  const isError = proposal.status === "error"
  const isOrganised = proposal.status === "organised"
  const isConflict = proposal.status === "conflict"
  const isCleanup = proposal.status === "cleanup"
  const isWeb = isCleanup && proposal.source === "wikipedia"

  return (
    <>
      <tr className={`proposal-row ${isOrganised ? "row-organised" : isConflict ? "row-conflict" : proposal.approved ? "row-approved" : "row-rejected"} ${isUnmatched ? "row-unmatched" : ""}`}>
        <td className="col-approve">
          {isOrganised && (
            <span className="organised-icon" title="Already in Plex format - no rename needed">
              <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
            </span>
          )}
          {isConflict && (
            <span className="conflict-icon" title="A file already exists at the target location">!</span>
          )}
          {!isOrganised && !isConflict && !isUnmatched && !isError && (
            <button
              className={`approve-btn ${proposal.approved ? "approved" : "rejected"}`}
              onClick={toggle}
              title={proposal.approved ? "Click to reject" : "Click to approve"}
            >
              {proposal.approved
                ? <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
                : <svg viewBox="0 0 14 14" fill="none"><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
              }
            </button>
          )}
          {!isOrganised && !isConflict && (isUnmatched || isError) && (
            <span className="unmatched-icon" title="No TMDB match found">?</span>
          )}
        </td>

        <td className="col-original">
          <span className="path-text">{proposal.rel_path}</span>
        </td>

        <td className="col-arrow">→</td>

        <td className="col-proposed">
          {isUnmatched ? (
            <span className="unmatched-label">No match found</span>
          ) : isError ? (
            <span className="error-label">{proposal.error}</span>
          ) : (
            <div className="proposed-names">
              {isOrganised && <span className="organised-label">Already organised</span>}
              {isConflict && <span className="conflict-label">Target already exists - duplicate or wrong match</span>}
              {isCleanup && (
                <span className="cleanup-label">
                  {isWeb ? "Matched via Wikipedia - no TMDB result" : "Cleaned from filename - no TMDB match"}
                </span>
              )}
              <span className="proposed-folder">{proposal.proposed_folder}/</span>
              <span className="proposed-file">{proposal.proposed_filename}</span>
            </div>
          )}
        </td>

        <td className="col-confidence">
          {isCleanup ? (
            isWeb
              ? <span className="web-badge" title="Title/year from Wikipedia">Web</span>
              : <span className="local-badge" title="Named from the filename/folder; not verified against TMDB">Local</span>
          ) : !isUnmatched && !isError && (
            <ConfidencePip value={proposal.confidence} />
          )}
        </td>

        <td className="col-tmdb">
          {proposal.tmdb_url ? (
            <a href={proposal.tmdb_url} target="_blank" rel="noreferrer" className="tmdb-link">
              TMDB ↗
            </a>
          ) : proposal.wiki_url && (
            <a href={proposal.wiki_url} target="_blank" rel="noreferrer" className="tmdb-link">
              Wiki ↗
            </a>
          )}
        </td>

        <td className="col-expand">
          <button
            className={`expand-btn ${expanded ? "expanded" : ""}`}
            onClick={() => setExpanded(!expanded)}
            title={isUnmatched || isError ? "Search manually" : "Alternatives / manual search"}
          >
            <svg viewBox="0 0 12 12" fill="none">
              <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </button>
        </td>
      </tr>

      {expanded && (
        <tr className="alt-row">
          <td colSpan={7}>
            <div className="alt-panel">
              {isConflict && (
                <div className="conflict-resolve">
                  <span className="alt-heading">A file already exists at the target</span>
                  <p className="conflict-help">
                    If this source is a leftover duplicate of an already-organised title,
                    delete it. If it was matched to the wrong title, search again below.
                  </p>
                  {!confirmDelete ? (
                    <button className="btn-danger" onClick={() => setConfirmDelete(true)} disabled={busy}>
                      Delete this duplicate source file
                    </button>
                  ) : (
                    <div className="confirm-inline">
                      <span>Permanently delete the source file?</span>
                      <button className="btn-ghost" onClick={() => setConfirmDelete(false)} disabled={busy}>Cancel</button>
                      <button className="btn-danger" onClick={deleteSource} disabled={busy}>
                        {busy ? "Deleting..." : "Yes, delete"}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {proposal.alternatives?.length > 0 && (
                <>
                  <span className="alt-heading">Alternative matches - click to use</span>
                  <div className="alt-list">
                    {proposal.alternatives.map((alt, i) => (
                      <button
                        key={i}
                        className="alt-item"
                        onClick={() => selectAlt(alt)}
                        disabled={busy}
                      >
                        <span className="alt-title">{alt.title}</span>
                        <span className="alt-year">{alt.year}</span>
                        <span className={`alt-type ${alt.type}`}>{alt.type === "movie" ? "Film" : "TV Series"}</span>
                        <ConfidencePip value={alt.confidence} />
                      </button>
                    ))}
                  </div>
                </>
              )}

              <div className="manual-search">
                <span className="alt-heading">Search manually</span>
                <div className="manual-search-row">
                  <input
                    className="search-input manual-title"
                    placeholder="Correct title (e.g. Apollo 11)"
                    value={manualTitle}
                    onChange={e => setManualTitle(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") manualSearch() }}
                  />
                  <input
                    className="search-input manual-year"
                    placeholder="Year"
                    value={manualYear}
                    onChange={e => setManualYear(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") manualSearch() }}
                  />
                  <button
                    className="btn-primary"
                    onClick={manualSearch}
                    disabled={busy || !manualTitle.trim()}
                  >
                    {busy ? "Searching..." : "Search"}
                  </button>
                </div>
                {searchError && <span className="field-error">{searchError}</span>}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

export default function ProposalTable({ proposals, onChange, root, apiKey }) {
  const [filter, setFilter] = useState("all") // all | matched | unmatched
  const [search, setSearch] = useState("")
  const [showOrganised, setShowOrganised] = useState(false)

  const organisedCount = useMemo(
    () => proposals.filter(p => p.status === "organised").length,
    [proposals]
  )

  const filtered = useMemo(() => {
    return proposals.filter(p => {
      if (!showOrganised && p.status === "organised") return false
      if (filter === "matched" && (!p.matched || p.status === "unmatched")) return false
      if (filter === "unmatched" && p.matched && p.status !== "unmatched") return false
      if (search) {
        const q = search.toLowerCase()
        return (
          p.rel_path?.toLowerCase().includes(q) ||
          p.proposed_filename?.toLowerCase().includes(q) ||
          p.matched_title?.toLowerCase().includes(q)
        )
      }
      return true
    })
  }, [proposals, filter, search])

  const updateRow = (index, updated) => {
    // index here is into filtered, map back to original
    const original = proposals.findIndex(p => p.full_path === filtered[index].full_path)
    const next = [...proposals]
    next[original] = updated
    onChange(next)
  }

  const removeRow = (index) => {
    const target = filtered[index].full_path
    onChange(proposals.filter(p => p.full_path !== target))
  }

  const approveAll = () => onChange(proposals.map(p =>
    p.status === "pending" || p.status === "cleanup" ? { ...p, approved: true } : p
  ))
  const rejectAll = () => onChange(proposals.map(p => ({ ...p, approved: false })))

  return (
    <div className="proposal-panel">
      <div className="proposal-toolbar">
        <div className="toolbar-left">
          <input
            className="search-input"
            placeholder="Filter files..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          <div className="filter-tabs">
            {["all", "matched", "unmatched"].map(f => (
              <button
                key={f}
                className={`filter-tab ${filter === f ? "active" : ""}`}
                onClick={() => setFilter(f)}
              >
                {f.charAt(0).toUpperCase() + f.slice(1)}
              </button>
            ))}
          </div>
          {organisedCount > 0 && (
            <label className="organised-toggle" title="Files already in Plex format that need no rename">
              <input
                type="checkbox"
                checked={showOrganised}
                onChange={e => setShowOrganised(e.target.checked)}
              />
              Show {organisedCount} already organised
            </label>
          )}
        </div>
        <div className="toolbar-right">
          <button className="btn-ghost" onClick={approveAll}>Approve all</button>
          <button className="btn-ghost" onClick={rejectAll}>Reject all</button>
        </div>
      </div>

      <div className="table-scroll">
        <table className="proposal-table">
          <thead>
            <tr>
              <th className="col-approve"></th>
              <th className="col-original">Current path</th>
              <th className="col-arrow"></th>
              <th className="col-proposed">Proposed Plex name</th>
              <th className="col-confidence">Confidence</th>
              <th className="col-tmdb">TMDB</th>
              <th className="col-expand"></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((p, i) => (
              <ProposalRow
                key={p.full_path}
                proposal={p}
                index={i}
                onChange={updateRow}
                onRemove={removeRow}
                root={root}
                apiKey={apiKey}
              />
            ))}
          </tbody>
        </table>

        {filtered.length === 0 && (
          <div className="table-empty">
            {!showOrganised && organisedCount > 0 && proposals.length === organisedCount
              ? `All ${organisedCount} files are already organised - nothing to rename.`
              : "No files match the current filter."}
          </div>
        )}
      </div>
    </div>
  )
}
