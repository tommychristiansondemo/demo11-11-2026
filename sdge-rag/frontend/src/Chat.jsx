import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { api } from './api.js';
import { FileIcon, Send } from './icons.jsx';

const SUGGESTIONS = [
  'What should I do first if I smell gas at a customer premises?',
  'What PPE is required for excavation near a gas main?',
  'How often must distribution leak surveys be performed?',
  'What are the reporting deadlines for an incident to PHMSA and the CPUC?',
  'What is the procedure for purging a newly installed service line?',
  'What is the policy on cell phone use while driving a company vehicle?',
];

function withCitationLinks(text) {
  return text.replace(/\[(\d{1,2})\](?!\()/g, '[$1](#cite-$1)');
}

async function openDoc(docId) {
  try {
    const { url } = await api(`/documents/${docId}/download`);
    window.open(url, '_blank', 'noopener');
  } catch (e) {
    alert(e.message);
  }
}

function Answer({ msg }) {
  const [open, setOpen] = useState(null);
  const components = {
    a: ({ href, children }) => {
      const m = /^#cite-(\d+)$/.exec(href || '');
      if (m) {
        const n = Number(m[1]);
        const src = msg.sources?.find((s) => s.n === n);
        return (
          <button className="cite" title={src ? src.docName : ''} onClick={() => setOpen(open === n ? null : n)}>{n}</button>
        );
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
  };
  const cited = new Set([...msg.content.matchAll(/\[(\d{1,2})\]/g)].map((m) => Number(m[1])));
  const sources = (msg.sources || []).filter((s) => cited.has(s.n));
  const shown = sources.length ? sources : [];
  return (
    <div className="answer">
      <div className="md"><ReactMarkdown components={components}>{withCitationLinks(msg.content)}</ReactMarkdown></div>
      {shown.length > 0 && (
        <div className="sources">
          <div className="sources-label">Sources</div>
          {shown.map((s) => (
            <div key={s.n} className={`source ${open === s.n ? 'open' : ''}`}>
              <button className="source-head" onClick={() => setOpen(open === s.n ? null : s.n)}>
                <span className="cite static">{s.n}</span>
                <span className="source-name">{s.docName}</span>
                <span className="source-meta">{s.category || 'KB'}{s.page ? ` · p.${s.page}` : ''} · {Math.round(s.score * 100)}% match</span>
              </button>
              {open === s.n && (
                <div className="source-body">
                  <p>{s.excerpt}{s.excerpt.length >= 700 ? '…' : ''}</p>
                  <button className="btn link small" onClick={() => openDoc(s.docId)}><FileIcon size={14} /> Open document</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {msg.timings && <div className="meta-line">{(msg.timings.totalMs / 1000).toFixed(1)}s · answered by {msg.modelName || msg.model} · retrieval: {msg.retriever === 'bedrock-kb' ? 'Bedrock Knowledge Base' : 'Aurora pgvector'}</div>}
    </div>
  );
}

export default function Chat({ user, onAuthError }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [retriever, setRetriever] = useState(() => { try { return localStorage.getItem('retriever') || 'pgvector'; } catch { return 'pgvector'; } });
  const pickRetriever = (v) => { setRetriever(v); try { localStorage.setItem('retriever', v); } catch { /* private mode */ } };
  const endRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages, busy]);

  async function ask(q) {
    const question = (q ?? input).trim();
    if (!question || busy) return;
    setInput('');
    const history = messages.filter((m) => !m.error).map(({ role, content }) => ({ role, content }));
    setMessages((m) => [...m, { role: 'user', content: question }]);
    setBusy(true);
    try {
      const r = await api('/chat', { method: 'POST', body: { question, history, retriever } });
      setMessages((m) => [...m, { role: 'assistant', content: r.answer, sources: r.sources, timings: r.timings, model: r.model, modelName: r.modelName, retriever: r.retriever }]);
    } catch (e) {
      if (e.status === 401) return onAuthError();
      setMessages((m) => [...m, { role: 'assistant', content: e.message, error: true }]);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="chat">
      <div className="chat-scroll">
        {messages.length === 0 && (
          <div className="empty">
            <h2>Hi {user.username}, what do you need to know?</h2>
            <p>Answers come only from documents in the knowledge base, with numbered citations you can open.</p>
            <div className="suggest">
              {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.error ? 'error' : ''}`}>
            {m.role === 'user' ? <div className="bubble">{m.content}</div> : <Answer msg={m} />}
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="thinking"><span /><span /><span /> Searching documents ({retriever === 'bedrock-kb' ? 'Bedrock KB' : 'pgvector'})…</div></div>}
        <div ref={endRef} />
      </div>
      <div className="retriever-bar" role="radiogroup" aria-label="Retrieval engine">
        <span className="muted small">Retrieval engine:</span>
        {[['pgvector', 'Aurora pgvector (custom pipeline)'], ['bedrock-kb', 'Bedrock Knowledge Base (managed, S3 Vectors)']].map(([v, label]) => (
          <button key={v} type="button" role="radio" aria-checked={retriever === v} disabled={busy}
                  className={`seg ${retriever === v ? 'on' : ''}`} onClick={() => pickRetriever(v)}>{label}</button>
        ))}
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); ask(); }}>
        <textarea ref={inputRef} value={input} rows={1} maxLength={4000} placeholder="Ask about procedures, safety, maintenance, compliance or policy…"
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } }} />
        <button className="btn primary icon" disabled={busy || !input.trim()} aria-label="Send"><Send /></button>
        {messages.length > 0 && <button type="button" className="btn ghost small" onClick={() => setMessages([])}>New chat</button>}
      </form>
    </div>
  );
}
