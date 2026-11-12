import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, AuthError } from './lib.js';

export default function Knowledge({ onAuthError }) {
  const [md, setMd] = useState('');
  const [err, setErr] = useState('');
  useEffect(() => {
    api('/knowledge').then((r) => setMd(r.markdown)).catch((e) => (e instanceof AuthError ? onAuthError() : setErr(e.message)));
  }, [onAuthError]);
  if (err) return <div className="page"><div className="alert">{err}</div></div>;
  return (
    <div className="page doc">
      <div className="doc-note">This is <code>projectknowledge.md</code> exactly as the chatbot reads it. It is stored in S3 and regenerated
        automatically whenever a CloudFormation stack changes.</div>
      {md ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown></div> : <p className="muted">Loading…</p>}
    </div>
  );
}
