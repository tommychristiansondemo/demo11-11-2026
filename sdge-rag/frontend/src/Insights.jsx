import { useEffect, useState } from 'react';
import { api, parseTs } from './api.js';

export default function Insights({ onAuthError }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    api('/stats').then(setData).catch((e) => (e.status === 401 ? onAuthError() : setError(e.message)));
  }, [onAuthError]);

  if (error) return <div className="alert error">{error}</div>;
  if (!data) return <div className="muted pad">Loading…</div>;
  const docs = Object.values(data.documentsByStatus).reduce((a, b) => a + b, 0);
  const tiles = [
    ['Documents', docs, `${data.documentsByStatus.ready || 0} indexed`],
    ['Indexed passages', data.chunks, 'pgvector · 1024-dim'],
    ['Questions asked', data.chats.total, `${data.chats.last24h} in last 24h`],
    ['Active users', data.chats.users, 'asked at least one question'],
    ['Avg. response', `${(data.chats.avg_latency_ms / 1000).toFixed(1)}s`, 'retrieval + generation'],
  ];
  return (
    <div className="insights">
      <h2>Usage</h2>
      <div className="tiles">
        {tiles.map(([label, value, sub]) => (
          <div key={label} className="tile"><div className="tile-label">{label}</div><div className="tile-value">{value}</div><div className="tile-sub">{sub}</div></div>
        ))}
      </div>
      <h3>Recent questions</h3>
      <div className="table-wrap">
        <table className="doc-table">
          <thead><tr><th>When</th><th>User</th><th>Question</th><th>Model</th><th className="num">Latency</th></tr></thead>
          <tbody>
            {data.recentQuestions.length === 0 && <tr><td colSpan={5} className="muted pad">No questions yet.</td></tr>}
            {data.recentQuestions.map((q, i) => (
              <tr key={i}>
                <td className="nowrap">{parseTs(q.created_at)?.toLocaleString()}</td>
                <td>{q.username}</td><td>{q.question}</td><td className="small">{q.model || '—'}</td><td className="num">{(q.latency_ms / 1000).toFixed(1)}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
