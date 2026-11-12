import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import { api } from './api.js';

const DIMS = [
  { key: 'quality', label: 'Quality', hint: 'Answer accuracy, grounding and citation discipline', color: 'var(--q)' },
  { key: 'cost', label: 'Cost', hint: 'Lower $ per question (real Bedrock on-demand prices × measured tokens)', color: 'var(--c)' },
  { key: 'latency', label: 'Latency', hint: 'Faster end-to-end response, measured live from Lambda', color: 'var(--l)' },
];
const DEFAULT = { quality: 50, cost: 25, latency: 25 };

// Move one slider; rebalance the other two proportionally so the total is always exactly 100.
export function rebalance(w, key, value) {
  const v = Math.max(0, Math.min(100, Math.round(value)));
  const [a, b] = DIMS.map((d) => d.key).filter((k) => k !== key);
  const rest = 100 - v;
  const sum = w[a] + w[b];
  const na = sum === 0 ? Math.floor(rest / 2) : Math.round((rest * w[a]) / sum);
  return { ...w, [key]: v, [a]: na, [b]: rest - na };
}

function composite(row, w) {
  let s = 0; let tw = 0;
  for (const d of DIMS) {
    const v = row.scores[d.key];
    if (v != null) { s += v * w[d.key]; tw += w[d.key]; }
  }
  return tw ? Math.round((s / tw) * 10) / 10 : 0;
}

const ago = (iso) => {
  if (!iso) return '';
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  return s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : new Date(iso).toLocaleString();
};

function Bar({ value, color }) {
  return <div className="lab-bar"><div style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%`, background: color }} /></div>;
}

export default function ModelLab({ onAuthError }) {
  const [state, setState] = useState(null);
  const [weights, setWeights] = useState(DEFAULT);
  const [busy, setBusy] = useState(false);
  const [remeasure, setRemeasure] = useState(false);
  const [decision, setDecision] = useState(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(null);

  const load = useCallback(async (resetSliders) => {
    try {
      const s = await api('/models');
      setState(s);
      if (resetSliders) setWeights(s.weights);
      setError('');
    } catch (e) {
      if (e.status === 401) return onAuthError();
      setError(e.message);
    }
  }, [onAuthError]);
  useEffect(() => { load(true); }, [load]);
  useEffect(() => { const t = setInterval(() => load(false), 20000); return () => clearInterval(t); }, [load]);

  const ranking = useMemo(() => {
    if (!state) return [];
    return state.ranking.map((r) => ({ ...r, preview: composite(r, weights) }))
      .sort((a, b) => b.preview - a.preview || a.name.localeCompare(b.name))
      .map((r, i) => ({ ...r, previewRank: i + 1 }));
  }, [state, weights]);

  const dirty = state && DIMS.some((d) => state.weights[d.key] !== weights[d.key]);
  const total = weights.quality + weights.cost + weights.latency;
  const active = state?.ranking.find((r) => r.active);

  async function evaluate() {
    setBusy(true); setDecision(null); setError('');
    try {
      const r = await api('/models/evaluate', { method: 'POST', body: { weights, forceRemeasure: remeasure } });
      setState(r);
      setWeights(r.weights);
      setDecision({ ...r.decision, benchmarkMs: r.benchmarkMs, reused: r.lastEvaluation?.reusedMeasurements });
    } catch (e) {
      if (e.status === 401) return onAuthError();
      setError(e.message);
    } finally { setBusy(false); }
  }

  if (!state) return <div className="lab"><p className="muted">{error || 'Loading Model Lab…'}</p></div>;
  const nameOf = (id) => state.ranking.find((r) => r.id === id)?.name || id;

  return (
    <div className="lab">
      <div className="lab-head">
        <div>
          <h2>Model Lab</h2>
          <p className="muted">Amazon Bedrock lets one application call many vendors&apos; models through the same Converse API.
            Set your priorities, then evaluate. The app benchmarks all eight models live and, if another model is a better fit,
            switches the <strong>live</strong> Gas Ops assistant to it through AWS AppConfig. No redeploy needed.</p>
        </div>
        <div className="live-model">
          <div className="lm-label">Live model</div>
          <div className="lm-name">{active?.name}</div>
          <div className="muted small">{active?.vendor} · AppConfig v{state.configVersion}</div>
        </div>
      </div>

      <div className="lab-grid">
        <section className="card-lite sliders">
          <h3>Your preferences</h3>
          {DIMS.map((d) => (
            <div key={d.key} className="slider-row">
              <div className="slider-top">
                <label htmlFor={`w-${d.key}`}><span className="dot" style={{ background: d.color }} />{d.label}</label>
                <span className="slider-val">{weights[d.key]}%</span>
              </div>
              <input id={`w-${d.key}`} type="range" min="0" max="100" step="1" value={weights[d.key]}
                     style={{ '--fill': d.color }} disabled={busy}
                     onChange={(e) => setWeights(rebalance(weights, d.key, Number(e.target.value)))} />
              <div className="muted small">{d.hint}</div>
            </div>
          ))}
          <div className="split" aria-label="weight split">
            {DIMS.map((d) => <div key={d.key} style={{ width: `${weights[d.key]}%`, background: d.color }} title={`${d.label} ${weights[d.key]}%`} />)}
          </div>
          <div className="total">Total: <strong>{total}%</strong></div>
          <label className="check"><input type="checkbox" checked={remeasure} onChange={(e) => setRemeasure(e.target.checked)} disabled={busy} />
            Re-measure now (otherwise measurements under 2 minutes old are reused)</label>
          <div className="lab-actions">
            <button className="btn primary" onClick={evaluate} disabled={busy || total !== 100}>
              {busy ? 'Benchmarking 8 models live…' : 'Evaluate & apply'}
            </button>
            <button className="btn ghost small" disabled={busy} onClick={() => setWeights(DEFAULT)}>Reset 50 / 25 / 25</button>
            {dirty && !busy && <button className="btn ghost small" onClick={() => setWeights(state.weights)}>Revert to saved</button>}
          </div>
          {busy && <p className="muted small">Each model answers two grounded-QA test questions in parallel using the app&apos;s real system prompt. This takes 5–20 seconds.</p>}
          {error && <div className="alert error">{error}</div>}
        </section>

        <section className="card-lite">
          <h3>Current configuration</h3>
          <dl className="kv">
            <dt>Saved weights</dt><dd>Quality {state.weights.quality}% · Cost {state.weights.cost}% · Latency {state.weights.latency}%</dd>
            <dt>Last evaluation</dt><dd>{state.lastEvaluation?.at ? `${ago(state.lastEvaluation.at)} by ${state.lastEvaluation.by}` : 'never'}</dd>
            <dt>Measured</dt><dd>{state.lastEvaluation?.measuredAt ? ago(state.lastEvaluation.measuredAt) : 'not yet'}</dd>
            <dt>Switch rule</dt><dd>Best eligible model must beat the live one by ≥ {state.policy?.switchMarginPoints} points</dd>
            <dt>Eligibility</dt><dd>Passes the grounded-answer test and responds in under {(state.policy?.maxLatencyMs || 20000) / 1000}s</dd>
            <dt>AppConfig</dt><dd className="mono small">{state.appconfig.application} / {state.appconfig.environment} / {state.appconfig.profile}</dd>
          </dl>
          {decision && (
            <div className={`decision ${decision.action}`}>
              <strong>{decision.action === 'switch' ? `Switched: ${nameOf(decision.from)} → ${nameOf(decision.to)}`
                : decision.action === 'keep' ? `Kept ${nameOf(decision.to)}` : 'Measurements refreshed'}</strong>
              <div>{decision.reason}</div>
              <div className="muted small">{decision.reused ? 'Reused recent measurements' : `Live benchmark took ${(decision.benchmarkMs / 1000).toFixed(1)}s`}
                {decision.action === 'switch' && ' · The chat API picks up the change within ~15 seconds.'}</div>
            </div>
          )}
        </section>
      </div>

      <h3 className="rank-title">Ranking {dirty ? <span className="preview-tag">previewing your unsaved sliders</span> : <span className="muted small">for the saved preferences</span>}</h3>
      <div className="table-wrap">
        <table className="doc-table lab-table">
          <thead>
            <tr><th>#</th><th>Model</th><th>Quality</th><th>Cost</th><th>Latency</th><th>Grounding tests</th><th>Score</th><th>Status</th></tr>
          </thead>
          <tbody>
            {ranking.map((r) => (
              <Fragment key={r.id}>
                <tr className={r.active ? 'is-active' : ''} onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td className="num">{r.previewRank}</td>
                  <td>
                    <div className="m-name">{r.name}</div>
                    <div className="muted small">{r.vendor}{r.openWeights ? ' · open weights' : ''}{r.reasoning !== 'none' ? ` · ${r.reasoning === 'native' ? 'native reasoning' : 'extended thinking'}` : ''}</div>
                  </td>
                  <td><Bar value={r.scores.quality} color="var(--q)" /><span className="small">prior {r.qualityPrior}{r.liveQuality != null ? `, live ${r.liveQuality}` : ''}</span></td>
                  <td><Bar value={r.scores.cost} color="var(--c)" /><span className="small">${r.costPer1kQuestionsUsd.toFixed(2)} / 1k questions</span></td>
                  <td><Bar value={r.scores.latency} color="var(--l)" /><span className="small">{r.latencyMs != null ? `${(r.latencyMs / 1000).toFixed(2)}s` : '—'}</span></td>
                  <td className="small nowrap">{r.tests ? <>answer {r.tests.answerable ? '✅' : '❌'} · decline {r.tests['must-decline'] ? '✅' : '❌'}</> : '—'}</td>
                  <td className="score">{r.preview.toFixed(1)}</td>
                  <td>{r.active ? <span className="status ready">Live</span>
                    : r.eligible ? <span className="status eligible">Eligible</span>
                    : <span className="status failed" title={r.ineligibleReasons.join('; ')}>Ineligible</span>}</td>
                </tr>
                {open === r.id && (
                  <tr className="detail-row">
                    <td />
                    <td colSpan={7}>
                      <div className="small"><strong>Strengths:</strong> {r.strengths}</div>
                      <div className="small"><strong>Bedrock model ID:</strong> <code>{r.id}</code> · on-demand ${r.pricePer1M.input} in / ${r.pricePer1M.output} out per 1M tokens</div>
                      {r.ineligibleReasons.length > 0 && <div className="small err-text">Not eligible: {r.ineligibleReasons.join('; ')}</div>}
                      {r.error && <div className="small err-text">{r.error}</div>}
                      {r.answerPreview && <div className="small"><strong>Benchmark answer:</strong> <em>{r.answerPreview}</em></div>}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Scores are normalised 0–100 across the eight models. Quality = 75% benchmark-informed prior + 25% live grounding tests.
        Cost = measured tokens × AWS Price List on-demand rates (log scale). Latency = measured mean response time (log scale).
        Composite = weighted average of the three. Click a row for details.</p>

      {state.history?.length > 0 && (
        <>
          <h3>Recent changes</h3>
          <div className="table-wrap">
            <table className="doc-table">
              <thead><tr><th>When</th><th>By</th><th>Weights (Q/C/L)</th><th>Decision</th><th>Top 3</th></tr></thead>
              <tbody>{state.history.map((h, i) => (
                <tr key={i}><td className="nowrap">{ago(h.at)}</td><td>{h.by}</td><td>{h.weights.quality}/{h.weights.cost}/{h.weights.latency}</td>
                  <td>{h.action === 'switch' ? `${nameOf(h.from)} → ${nameOf(h.to)}` : h.action === 'keep' ? `kept ${nameOf(h.to)}` : h.action}</td>
                  <td className="small">{h.top3.join(', ')}</td></tr>))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
