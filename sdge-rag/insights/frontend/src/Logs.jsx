import { useCallback, useEffect, useState } from 'react';
import { api, AuthError, fmtTime } from './lib.js';

const LABELS = {
  'main-api': 'Gas Ops · API service (Fargate)', 'main-ingest': 'Gas Ops · ingest tasks (Fargate)', 'main-presignup': 'Gas Ops · Cognito pre-sign-up',
  'main-postconfirm': 'Gas Ops · Cognito post-confirmation', 'main-dbinit': 'Gas Ops · DB schema init', 'main-apigw-access': 'Gas Ops · API Gateway access log',
  'pipeline-validate': 'CI/CD · validate build', 'pipeline-deploy': 'CI/CD · deploy build', 'insights-chat': 'Class Insights · chat service (Fargate)', 'image-builder': 'CI/CD · container image builder',
  'insights-data': 'Class Insights · data Lambda', 'insights-knowledge': 'Class Insights · knowledge generator', 'insights-apigw-access': 'Class Insights · API access log',
  'legacy-main-api-lambda': 'Legacy · API Lambda (before Fargate)', 'legacy-main-ingest-lambda': 'Legacy · ingest Lambda', 'legacy-insights-chat-lambda': 'Legacy · chatbot Lambda',
};

export default function Logs({ onAuthError }) {
  const [component, setComponent] = useState('main-api');
  const [q, setQ] = useState('');
  const [level, setLevel] = useState('');
  const [minutes, setMinutes] = useState(1440);
  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const load = useCallback(() => {
    setData(null); setErr('');
    const p = new URLSearchParams({ component, q, level, minutes, limit: 100 });
    api(`/logs?${p}`).then(setData).catch((e) => (e instanceof AuthError ? onAuthError() : setErr(e.message)));
  }, [component, q, level, minutes, onAuthError]);
  useEffect(load, [component, level, minutes]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="page">
      <div className="page-head">
        <div><h2>Logs</h2><p className="muted">Recent CloudWatch Logs events (structured JSON). Tokens and credentials are redacted.</p></div>
      </div>
      <form className="controls wrap" onSubmit={(e) => { e.preventDefault(); load(); }}>
        <select value={component} onChange={(e) => setComponent(e.target.value)}>
          {Object.entries(LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">Any level</option><option value="ERROR">Errors</option><option value="WARNING">Warnings</option><option value="INFO">Info</option>
        </select>
        <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
          <option value={15}>Last 15 min</option><option value={60}>Last hour</option><option value={1440}>Last 24 h</option><option value={10080}>Last 7 days</option>
        </select>
        <input placeholder="Filter text (e.g. document indexed)" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="btn ghost small">Search</button>
      </form>
      {err && <div className="alert">{err}</div>}
      {!data ? <p className="muted">Loading…</p> : (
        <>
          <p className="muted small">{data.count} events from <code>{data.logGroup}</code>{data.note ? ` — ${data.note}` : ''}</p>
          <div className="logbox">
            {data.events.map((e, i) => <div key={i} className="logline"><span className="ts">{fmtTime(e.time)}</span><code>{e.message}</code></div>)}
          </div>
        </>
      )}
    </div>
  );
}
