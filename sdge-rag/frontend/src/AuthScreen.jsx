import { useState } from 'react';
import { signIn, signUp, startSso } from './auth.js';
import { Flame, Shield } from './icons.jsx';

const cfg = window.APP_CONFIG || {};

export default function AuthScreen({ onSignedIn }) {
  const [mode, setMode] = useState('signin');
  const [form, setForm] = useState({ username: '', password: '', role: 'employee' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      if (mode === 'register') {
        await signUp(form);
      }
      onSignedIn(await signIn(form.username.trim(), form.password));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-hero">
        <Flame size={44} />
        <h1>Gas Ops Knowledge Assistant</h1>
        <p>Ask questions in plain language about gas operations, safety procedures, maintenance protocols,
          regulatory compliance and company policy — answers are grounded in approved internal documents, with citations.</p>
        <ul className="hero-points">
          <li>Grounded answers with source citations</li>
          <li>Administrators curate the document library</li>
          <li>Restricted to authenticated employees</li>
        </ul>
      </div>
      <form className="auth-card" onSubmit={submit}>
        <div className="seg">
          <button type="button" className={mode === 'signin' ? 'on' : ''} onClick={() => { setMode('signin'); setError(''); }}>Sign in</button>
          <button type="button" className={mode === 'register' ? 'on' : ''} onClick={() => { setMode('register'); setError(''); }}>Create account</button>
        </div>

        {mode === 'register' && (
          <>
            <fieldset className="role-pick">
              <legend>Account type</legend>
              {[
                ['employee', 'Employee', 'Ask questions and browse documents'],
                ['admin', 'Administrator', 'Also upload, re-index and delete documents'],
              ].map(([v, label, hint]) => (
                <label key={v} className={form.role === v ? 'role-opt on' : 'role-opt'}>
                  <input type="radio" name="role" value={v} checked={form.role === v} onChange={set('role')} />
                  <span><strong>{label}</strong><small>{hint}</small></span>
                </label>
              ))}
            </fieldset>
          </>
        )}

        <label className="field">
          <span>Username</span>
          <input value={form.username} onChange={set('username')} autoComplete="username" required
                 pattern="\S+" title="No spaces" maxLength={64} />
        </label>
        <label className="field">
          <span>Password</span>
          <input type="password" value={form.password} onChange={set('password')} required minLength={6}
                 autoComplete={mode === 'register' ? 'new-password' : 'current-password'} />
        </label>
        {mode === 'register' && <p className="hint">Demo environment: pick any username and a 6+ character password. Register as many accounts of each type as you like.</p>}
        {error && <div className="alert error" role="alert">{error}</div>}
        <button className="btn primary block" disabled={busy}>
          {busy ? 'Please wait…' : mode === 'register' ? 'Create account & sign in' : 'Sign in'}
        </button>
        {cfg.ssoProvider && (
          <>
            <div className="or"><span>or</span></div>
            <button type="button" className="btn ghost block" onClick={startSso}><Shield /> Sign in with corporate SSO</button>
          </>
        )}
      </form>
    </div>
  );
}
