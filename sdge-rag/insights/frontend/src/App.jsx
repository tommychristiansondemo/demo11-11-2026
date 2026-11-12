import { useCallback, useEffect, useState } from 'react';
import { getSession, signIn, signOut } from './lib.js';
import Ask from './Ask.jsx';
import Knowledge from './Knowledge.jsx';
import Traces from './Traces.jsx';
import Logs from './Logs.jsx';
import Status from './Status.jsx';

const cfg = window.APP_CONFIG || {};
const TABS = [
  ['ask', 'Ask'], ['knowledge', 'Architecture & design'], ['traces', 'LLM traces'], ['logs', 'Logs'], ['status', 'Live status'],
];

function Login({ onIn }) {
  const [u, setU] = useState('');
  const [p, setP] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr('');
    try { onIn(await signIn(u.trim(), p)); } catch (x) { setErr(x.message); } finally { setBusy(false); }
  }
  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <div className="logo-row"><img src="/favicon.svg" alt="" width="36" height="36" /><h1>Class Insights</h1></div>
        <p className="muted">See how the Gas Ops Knowledge Assistant was designed, built and deployed on AWS: architecture,
          live resources, logs and every LLM prompt with its reasoning.</p>
        <p className="note">Sign in with the <strong>same username and password</strong> you use for the Gas Ops Knowledge
          Assistant. Any account type (Employee or Administrator) gets full access.</p>
        <label className="field"><span>Username</span><input value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" required /></label>
        <label className="field"><span>Password</span><input type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" required /></label>
        {err && <div className="alert">{err}</div>}
        <button className="btn primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        {cfg.mainAppUrl && <p className="muted small">No account yet? <a href={cfg.mainAppUrl}>Create one in the Gas Ops Knowledge Assistant</a>.</p>}
      </form>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState(undefined);
  const [tab, setTab] = useState('ask');
  useEffect(() => { getSession().then(setSession); }, []);
  const onAuthError = useCallback(() => { signOut(); setSession(null); }, []);

  if (session === undefined) return null;
  if (!session) return <Login onIn={setSession} />;

  return (
    <div className="shell">
      <header className="top">
        <div className="logo-row"><img src="/favicon.svg" alt="" width="28" height="28" />
          <div><div className="title">Class Insights</div><div className="sub">Gas Ops RAG deployment · {cfg.env}</div></div>
        </div>
        <nav className="tabs">
          {TABS.map(([id, label]) => (
            <button key={id} className={tab === id ? 'tab on' : 'tab'} onClick={() => setTab(id)}>{label}</button>
          ))}
        </nav>
        <div className="who">
          {cfg.mainAppUrl && <a href={cfg.mainAppUrl} className="back">← Gas Ops app</a>}
          <span>{session.user.username}</span>
          <button className="btn ghost small" onClick={onAuthError}>Sign out</button>
        </div>
      </header>
      <main className="main">
        <div className="pane" hidden={tab !== 'ask'}><Ask onAuthError={onAuthError} /></div>
        {tab === 'knowledge' && <div className="pane"><Knowledge onAuthError={onAuthError} /></div>}
        {tab === 'traces' && <div className="pane"><Traces onAuthError={onAuthError} /></div>}
        {tab === 'logs' && <div className="pane"><Logs onAuthError={onAuthError} /></div>}
        {tab === 'status' && <div className="pane"><Status onAuthError={onAuthError} /></div>}
      </main>
    </div>
  );
}
