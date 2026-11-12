import { useCallback, useEffect, useRef, useState } from 'react';
import { api, parseTs, uploadToS3 } from './api.js';
import { FileIcon, Refresh, Trash, Upload } from './icons.jsx';

const TYPES = {
  pdf: 'application/pdf',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  txt: 'text/plain',
  md: 'text/markdown',
};
const fmtSize = (b) => (b == null ? '—' : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`);
const fmtDate = (d) => parseTs(d)?.toLocaleString() ?? '—';

function Status({ doc }) {
  const label = { ready: 'Indexed', processing: 'Processing', uploading: 'Uploading', queued: 'Queued', failed: 'Failed' }[doc.status] || doc.status;
  return <span className={`status ${doc.status}`} title={doc.error || ''}>{label}</span>;
}

export default function Documents({ user, onAuthError }) {
  const [docs, setDocs] = useState([]);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  const [category, setCategory] = useState('Safety Procedures');
  const [uploads, setUploads] = useState([]);
  const [kb, setKb] = useState(null);
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await api('/documents');
      setDocs(r.documents);
      setCategories(r.categories);
      setError('');
      api('/kb').then(setKb).catch(() => {});
    } catch (e) {
      if (e.status === 401) return onAuthError();
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [onAuthError]);

  useEffect(() => { load(); }, [load]);
  const kbBusy = kb?.jobs?.[0] && ['STARTING', 'IN_PROGRESS'].includes(kb.jobs[0].status);
  const pending = kbBusy || docs.some((d) => ['uploading', 'processing', 'queued'].includes(d.status));
  useEffect(() => {
    if (!pending) return undefined;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [pending, load]);

  async function uploadFiles(files) {
    for (const file of files) {
      const ext = file.name.split('.').pop().toLowerCase();
      const contentType = TYPES[ext];
      const id = `${file.name}-${Date.now()}`;
      const update = (patch) => setUploads((u) => u.map((x) => (x.id === id ? { ...x, ...patch } : x)));
      setUploads((u) => [...u, { id, name: file.name, progress: 0 }]);
      try {
        if (!contentType) throw new Error('Unsupported type (PDF, DOCX, TXT, MD only)');
        if (file.size > 25 * 1024 * 1024) throw new Error('File exceeds 25 MB');
        const r = await api('/documents', { method: 'POST', body: { name: file.name, contentType, size: file.size, category } });
        await load();
        await uploadToS3(r.uploadUrl, file, r.contentType, (p) => update({ progress: p }));
        update({ done: true });
        setTimeout(() => setUploads((u) => u.filter((x) => x.id !== id)), 2500);
        load();
      } catch (e) {
        update({ error: e.message });
      }
    }
    if (fileRef.current) fileRef.current.value = '';
  }

  async function remove(doc) {
    if (!confirm(`Delete "${doc.name}" and remove it from the knowledge base?`)) return;
    try { await api(`/documents/${doc.id}`, { method: 'DELETE' }); load(); } catch (e) { alert(e.message); }
  }
  async function reindex(doc) {
    try { await api(`/documents/${doc.id}/reindex`, { method: 'POST' }); load(); } catch (e) { alert(e.message); }
  }
  async function open(doc) {
    try { const { url } = await api(`/documents/${doc.id}/download`); window.open(url, '_blank', 'noopener'); } catch (e) { alert(e.message); }
  }

  const shown = docs.filter((d) => !filter || d.category === filter);
  const totalChunks = docs.reduce((n, d) => n + (d.chunk_count || 0), 0);

  return (
    <div className="docs">
      <div className="docs-head">
        <div>
          <h2>Document library</h2>
          <p className="muted">{docs.length} documents · {totalChunks} indexed passages
            {!user.isAdmin && ' · Sign in as an Administrator to upload or remove documents.'}</p>
        </div>
        <div className="docs-actions">
          <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by category">
            <option value="">All categories</option>
            {categories.map((c) => <option key={c}>{c}</option>)}
          </select>
          <button className="btn ghost small" onClick={load} aria-label="Refresh"><Refresh size={16} /></button>
        </div>
      </div>

      {user.isAdmin && (
        <div className="dropzone"
             onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add('over'); }}
             onDragLeave={(e) => e.currentTarget.classList.remove('over')}
             onDrop={(e) => { e.preventDefault(); e.currentTarget.classList.remove('over'); uploadFiles([...e.dataTransfer.files]); }}>
          <Upload size={22} />
          <div className="dz-text">
            <strong>Drop files to add them to the knowledge base</strong>
            <span>PDF, DOCX, TXT or Markdown · up to 25 MB · text is extracted, chunked and embedded automatically</span>
          </div>
          <select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category for new uploads">
            {(categories.length ? categories : [category]).map((c) => <option key={c}>{c}</option>)}
          </select>
          <button className="btn primary small" onClick={() => fileRef.current?.click()}>Choose files</button>
          <input ref={fileRef} type="file" multiple hidden accept=".pdf,.docx,.txt,.md" onChange={(e) => uploadFiles([...e.target.files])} />
        </div>
      )}
      {uploads.map((u) => (
        <div key={u.id} className={`upload-row ${u.error ? 'err' : ''}`}>
          <span>{u.name}</span>
          {u.error ? <span>{u.error}</span> : u.done ? <span>Uploaded — indexing…</span>
            : <div className="bar"><div style={{ width: `${Math.round(u.progress * 100)}%` }} /></div>}
        </div>
      ))}

      {error && <div className="alert error">{error}</div>}
      {loading ? <div className="muted pad">Loading…</div> : (
        <div className="table-wrap">
          <table className="doc-table">
            <thead>
              <tr><th>Document</th><th>Category</th><th>Status</th><th className="num">Passages</th><th className="num">Size</th><th>Uploaded</th><th /></tr>
            </thead>
            <tbody>
              {shown.length === 0 && <tr><td colSpan={7} className="muted pad">No documents yet.</td></tr>}
              {shown.map((d) => (
                <tr key={d.id}>
                  <td><button className="doc-name" onClick={() => open(d)}><FileIcon size={15} /> {d.name}</button>
                    {d.status === 'failed' && <div className="err-text">{d.error}</div>}</td>
                  <td>{d.category}</td>
                  <td><Status doc={d} /></td>
                  <td className="num">{d.chunk_count || '—'}</td>
                  <td className="num">{fmtSize(d.size_bytes)}</td>
                  <td className="nowrap"><div>{fmtDate(d.uploaded_at)}</div><div className="muted small">by {d.uploaded_by}</div></td>
                  <td className="row-actions">
                    {user.isAdmin && <>
                      <button className="btn ghost tiny" title="Re-index" onClick={() => reindex(d)}><Refresh size={14} /></button>
                      <button className="btn ghost tiny danger" title="Delete" onClick={() => remove(d)}><Trash size={14} /></button>
                    </>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {kb?.enabled && (
        <div className="kb-panel">
          <h3>Bedrock Knowledge Base sync</h3>
          <p className="muted small">The same files are also indexed by a managed <strong>Amazon Bedrock Knowledge Base</strong> ({kb.name}, {kb.vectorStore},
            {' '}{kb.embeddingModel}, {kb.chunking}). Every upload, re-index and delete starts an incremental ingestion job.
            Choose the retrieval engine in the Ask tab to compare it with the custom Aurora pgvector pipeline.</p>
          <table className="doc-table">
            <thead><tr><th>Started</th><th>Status</th><th>Trigger</th><th className="num">Scanned</th><th className="num">New / modified</th><th className="num">Deleted</th><th className="num">Failed</th></tr></thead>
            <tbody>{kb.jobs.map((j) => (
              <tr key={j.id}><td className="nowrap">{new Date(j.startedAt).toLocaleString()}</td>
                <td><span className={`status ${j.status === 'COMPLETE' ? 'ready' : j.status === 'FAILED' ? 'failed' : 'processing'}`}>{j.status.toLowerCase().replace('_', ' ')}</span></td>
                <td className="small">{j.description || '—'}</td>
                <td className="num">{j.stats.numberOfDocumentsScanned ?? '—'}</td>
                <td className="num">{(j.stats.numberOfNewDocumentsIndexed ?? 0) + (j.stats.numberOfModifiedDocumentsIndexed ?? 0)}</td>
                <td className="num">{j.stats.numberOfDocumentsDeleted ?? 0}</td>
                <td className="num">{j.stats.numberOfDocumentsFailed ?? 0}</td></tr>))}
              {kb.jobs.length === 0 && <tr><td colSpan={7} className="muted">No sync jobs yet.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
