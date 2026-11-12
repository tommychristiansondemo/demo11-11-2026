import { useEffect, useState } from 'react';
import { api, AuthError, fmtTime } from './lib.js';

const METRIC_LABELS = {
  chat_requests: 'Questions answered', input_tokens: 'LLM input tokens', output_tokens: 'LLM output tokens',
  llm_latency_ms_p90: 'LLM latency p90 (ms)', retrieval_top_score_avg: 'Avg. top retrieval score', documents_ingested: 'Documents ingested',
  chunks_indexed: 'Chunks indexed', api_requests: 'API requests', api_4xx: 'API 4XX', api_5xx: 'API 5XX',
  api_latency_ms_p90: 'API latency p90 (ms)', api_service_cpu_pct: 'API service CPU % (Fargate)', api_service_memory_pct: 'API service memory % (Fargate)', api_error_logs: 'API errors logged', ingest_failures: 'Ingest task failures',
  aurora_acu_avg: 'Aurora ACUs (avg)', insights_chat_cpu_pct: 'Insights chat CPU % (Fargate)',
};

export default function Status({ onAuthError }) {
  const [inv, setInv] = useState(null);
  const [alarms, setAlarms] = useState(null);
  const [pipe, setPipe] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [open, setOpen] = useState(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    const h = (e) => (e instanceof AuthError ? onAuthError() : setErr(e.message));
    api('/inventory?details=1').then(setInv).catch(h);
    api('/alarms').then(setAlarms).catch(h);
    api('/pipeline').then(setPipe).catch(h);
    api('/metrics?hours=24').then(setMetrics).catch(h);
  }, [onAuthError]);

  return (
    <div className="page">
      <h2>Live status</h2>
      {err && <div className="alert">{err}</div>}
      {inv && (
        <div className="tiles">
          <div className="tile"><div className="tl">CloudFormation stacks</div><div className="tv">{inv.totals.stacks}</div></div>
          <div className="tile"><div className="tl">Managed resources</div><div className="tv">{inv.totals.resources}</div></div>
          <div className="tile"><div className="tl">Distinct resource types</div><div className="tv">{inv.totals.distinctResourceTypes}</div></div>
          <div className="tile"><div className="tl">AWS services</div><div className="tv">{inv.totals.awsServices.length}</div></div>
        </div>
      )}
      <h3>Metrics — last 24 hours</h3>
      {!metrics ? <p className="muted">Loading…</p> : (
        <div className="tiles small-tiles">
          {Object.entries(metrics.metrics).map(([k, v]) => (
            <div key={k} className="tile"><div className="tl">{METRIC_LABELS[k] || k}</div><div className="tv">{v.value ?? '—'}</div></div>
          ))}
        </div>
      )}
      <h3>Stacks &amp; resources</h3>
      {!inv ? <p className="muted">Loading…</p> : inv.stacks.map((s) => (
        <div key={s.stack} className="card">
          <button className="stack-head" onClick={() => setOpen(open === s.stack ? null : s.stack)}>
            <strong>{s.stack}</strong><span className="muted">{s.region}</span><span className={`badge ${s.status.endsWith('COMPLETE') ? 'ok' : 'warn'}`}>{s.status}</span>
            <span className="muted small">{s.resourceCount} resources · updated {fmtTime(s.lastUpdated)}</span><span className="caret">{open === s.stack ? '▾' : '▸'}</span>
          </button>
          <p className="muted small">{s.description}</p>
          {open === s.stack && (
            <div className="table-wrap">
              <table className="tbl"><thead><tr><th>Logical ID</th><th>Type</th><th>Physical ID</th></tr></thead>
                <tbody>{s.resources.map((r) => <tr key={r.logicalId}><td>{r.logicalId}</td><td><code>{r.type}</code></td><td className="mono small">{r.physicalId}</td></tr>)}</tbody>
              </table>
            </div>
          )}
        </div>
      ))}
      <h3>Alarms</h3>
      {!alarms ? <p className="muted">Loading…</p> : (
        <div className="table-wrap"><table className="tbl"><thead><tr><th>Alarm</th><th>State</th><th>Metric</th><th>Description</th></tr></thead>
          <tbody>{alarms.alarms.map((a) => <tr key={a.name}><td>{a.name}</td><td><span className={`badge ${a.state === 'OK' ? 'ok' : a.state === 'ALARM' ? 'bad' : 'warn'}`}>{a.state}</span></td><td><code>{a.metric}</code></td><td>{a.description}</td></tr>)}</tbody>
        </table></div>
      )}
      <h3>CI/CD pipeline</h3>
      {!pipe ? <p className="muted">Loading…</p> : (
        <>
          <p className="muted small">{pipe.pipeline}: {pipe.stages?.map((s) => `${s.stage} ${s.status || '—'}`).join(' → ')}</p>
          <div className="table-wrap"><table className="tbl"><thead><tr><th>Execution</th><th>Status</th><th>Started</th><th>Trigger</th></tr></thead>
            <tbody>{pipe.runs.map((r) => <tr key={r.id}><td className="mono">{r.id.slice(0, 8)}</td><td>{r.status}</td><td>{fmtTime(r.start)}</td><td>{r.trigger}</td></tr>)}</tbody>
          </table></div>
        </>
      )}
    </div>
  );
}
