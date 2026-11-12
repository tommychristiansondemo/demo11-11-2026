import { useEffect, useState } from 'react';
import { completeSsoIfReturning, getValidSession, signOut } from './auth.js';
import AuthScreen from './AuthScreen.jsx';
import Chat from './Chat.jsx';
import Documents from './Documents.jsx';
import Insights from './Insights.jsx';
import ModelLab from './ModelLab.jsx';
import { Flame } from './icons.jsx';

const cfg = window.APP_CONFIG || {};

export default function App() {
  const [session, setSession] = useState(undefined);
  const [tab, setTab] = useState('ask');

  useEffect(() => {
    (async () => {
      try {
        const sso = await completeSsoIfReturning();
        setSession(sso || (await getValidSession()));
      } catch {
        setSession(null);
      }
    })();
  }, []);

  if (session === undefined) return <div className="boot"><Flame size={36} /></div>;
  if (!session) return <AuthScreen onSignedIn={setSession} />;

  const { user } = session;
  const logout = () => { signOut(); setSession(null); setTab('ask'); };
  const tabs = [['ask', 'Ask'], ['docs', 'Documents'], ['lab', 'Model Lab'], ...(user.isAdmin ? [['insights', 'Usage']] : [])];

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <Flame size={26} />
          <div>
            <div className="brand-title">Gas Ops Knowledge Assistant</div>
            <div className="brand-sub">SDG&amp;E internal · {cfg.env}</div>
          </div>
        </div>
        <nav className="tabs" role="tablist">
          {tabs.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'tab active' : 'tab'}
                    onClick={() => setTab(id)}>{label}</button>
          ))}
        </nav>
        {cfg.insightsUrl && (
          <a className="insights-link" href={cfg.insightsUrl} target="_blank" rel="noopener" title="How this app was designed and deployed (sign in with the same account)">
            Class Insights ↗
          </a>
        )}
        <div className="userbox">
          <div className="user-meta">
            <span className="user-name">{user.username}</span>
            <span className={`role-badge ${user.isAdmin ? 'admin' : ''}`}>{user.isAdmin ? 'Administrator' : 'Employee'}</span>
          </div>
          <button className="btn ghost small" onClick={logout}>Sign out</button>
        </div>
      </header>
      <main className="main">
        <div hidden={tab !== 'ask'} className="pane"><Chat user={user} onAuthError={logout} /></div>
        {tab === 'docs' && <div className="pane"><Documents user={user} onAuthError={logout} /></div>}
        {tab === 'lab' && <div className="pane"><ModelLab user={user} onAuthError={logout} /></div>}
        {tab === 'insights' && <div className="pane"><Insights onAuthError={logout} /></div>}
      </main>
      <footer className="foot">
        Training demo environment — sample documents are fictional and not affiliated with or endorsed by SDG&amp;E.
        In a real gas emergency, leave the area and call 911.
      </footer>
    </div>
  );
}
