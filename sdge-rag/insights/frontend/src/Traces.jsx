import { useCallback, useEffect, useState } from 'react';
import { api, AuthError, fmtTime } from './lib.js';

function Section({ title, children, open = false }) {
  return <details className="sec" open={open}><summary>{title}</summary>{children}</details>;
}

function Trace({ t }) {
  return (
    <div className="card trace">
      <div className="trace-head">
        <span className={`badge ${t.app === 'class-insights' ? 'b2' : t.type === 'model-benchmark' ? 'b3' : 'b1'}`}>
          {t.app === 'class-insights' ? 'Class Insights chatbot' : t.type === 'model-benchmark' ? 'Model Lab benchmark' : 'Gas Ops RAG'}</span>
        <strong className="q">{t.question}</strong>
      </div>
      <div className="trace-meta muted small">
        {fmtTime(t.time)} · user {t.user} · {t.model?.replace(/^us\.anthropic\./, '')} ·
        {' '}{t.usage?.inputTokens} in / {t.usage?.outputTokens} out tokens · {t.latencyMs?.total ?? '—'} ms total
        {t.latencyMs?.retrieval != null && ` (retrieval ${t.latencyMs.retrieval} ms, LLM ${t.latencyMs.llm} ms)`}
      </div>
      {t.retrieval && (
        <Section title={`Retrieval: ${t.retrieval.hits.length} passages (top-k ${t.retrieval.topK}, min score ${t.retrieval.minScore})`}>
          <table className="tbl"><thead><tr><th>#</th><th>Document</th><th>Chunk</th><th>Page</th><th className="num">Cosine similarity</th></tr></thead>
            <tbody>{t.retrieval.hits.map((h) => <tr key={h.n}><td>{h.n}</td><td>{h.doc}</td><td>{h.chunk}</td><td>{h.page ?? '—'}</td><td className="num">{h.score.toFixed(3)}</td></tr>)}</tbody>
          </table>
        </Section>
      )}
      {t.toolCalls?.length > 0 && (
        <Section title={`Tool calls (${t.toolCalls.length})`}>
          {t.toolCalls.map((c, i) => <div key={i} className="tool-row"><code>{c.name}({JSON.stringify(c.input)})</code>{c.ms != null && <span className="muted small"> {c.ms} ms</span>}
            {c.resultPreview && <pre className="pre small">{c.resultPreview}</pre>}</div>)}
        </Section>
      )}
      <Section title="Model reasoning (extended thinking)" open>
        <pre className="reasoning">{t.reasoning || '(no reasoning recorded)'}</pre>
      </Section>
      <Section title="Response"><pre className="pre">{t.response}</pre></Section>
      <Section title="System prompt"><pre className="pre">{t.systemPrompt}</pre></Section>
      <Section title="Prompt messages sent to the model">
        {(t.promptMessages || []).map((m, i) => <div key={i}><div className="role">{m.role}</div><pre className="pre">{m.text}</pre></div>)}
      </Section>
      <Section title="Inference configuration"><pre className="pre">{JSON.stringify({ ...t.inferenceConfig, stopReason: t.stopReason }, null, 2)}</pre></Section>
    </div>
  );
}

export default function Traces({ onAuthError }) {
  const [app, setApp] = useState('all');
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const load = useCallback(() => {
    setData(null);
    api(`/traces?app=${app}&limit=15&detail=full`).then(setData).catch((e) => (e instanceof AuthError ? onAuthError() : setErr(e.message)));
  }, [app, onAuthError]);
  useEffect(load, [load]);
  return (
    <div className="page">
      <div className="page-head">
        <div><h2>LLM traces</h2><p className="muted">Every prompt sent to Claude by either app, with retrieval evidence, tool calls and the model&apos;s reasoning. Read from CloudWatch Logs (<code>llm_trace</code> records).</p></div>
        <div className="controls">
          <select value={app} onChange={(e) => setApp(e.target.value)}>
            <option value="all">Both apps</option><option value="main">Gas Ops RAG assistant</option><option value="insights">Class Insights chatbot</option>
          </select>
          <button className="btn ghost small" onClick={load}>Refresh</button>
        </div>
      </div>
      {err && <div className="alert">{err}</div>}
      {!data ? <p className="muted">Loading…</p> : data.traces.length === 0 ? <p className="muted">No traces yet. Ask a question in either app.</p>
        : data.traces.map((t, i) => <Trace key={i} t={t} />)}
    </div>
  );
}
