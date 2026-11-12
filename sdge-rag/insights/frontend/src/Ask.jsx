import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, AuthError } from './lib.js';

const SUGGESTIONS = [
  'Give me a tour of the overall architecture.',
  'How many different types of AWS resources are in use?',
  'Which LLM does the Gas Ops assistant use, and why that one?',
  'Where does the assistant get its data, and how is a document ingested?',
  'Show me the last 3 LLM prompts from the Gas Ops app with their reasoning.',
  'Have there been any errors in the last 24 hours?',
  'How are API keys and database passwords protected?',
  'Which LLM is live right now, and who changed it last?',
  'How does the Model Lab decide which LLM to use?',
  'Which parts run on Fargate vs Lambda, and why?',
];

const TOOL_LABEL = {
  search_logs: 'CloudWatch Logs', get_llm_traces: 'LLM trace logs', get_metrics: 'CloudWatch metrics',
  list_resources: 'CloudFormation inventory', get_alarms: 'CloudWatch alarms', get_pipeline_runs: 'CodePipeline',
  get_document_library: 'S3 document library', get_model_selection: 'AppConfig (Model Lab)',
  search_project_kb: 'Bedrock KB (project corpus)', get_fargate_status: 'ECS Fargate', get_knowledge_bases: 'Bedrock KB status',
};

function Answer({ m }) {
  const [showR, setShowR] = useState(false);
  return (
    <div className="answer">
      {m.toolCalls?.length > 0 && (
        <div className="tools">
          <span className="tools-label">Fetched live:</span>
          {m.toolCalls.map((t, i) => (
            <span key={i} className="tool-chip" title={JSON.stringify(t.input)}>{TOOL_LABEL[t.name] || t.name} · {t.ms} ms</span>
          ))}
        </div>
      )}
      <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown></div>
      <div className="answer-foot">
        {m.reasoning && <button className="btn link small" onClick={() => setShowR(!showR)}>{showR ? 'Hide' : 'Show'} model reasoning</button>}
        {m.latencyMs != null && <span className="muted small">{(m.latencyMs / 1000).toFixed(1)}s · {m.usage?.inputTokens} in / {m.usage?.outputTokens} out tokens · trace {m.traceId}</span>}
      </div>
      {showR && <pre className="reasoning">{m.reasoning}</pre>}
    </div>
  );
}

export default function Ask({ onAuthError }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const end = useRef(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth' }); }, [msgs, busy]);

  async function ask(q) {
    const question = (q ?? input).trim();
    if (!question || busy) return;
    setInput('');
    const history = msgs.filter((m) => !m.error).map(({ role, content }) => ({ role, content }));
    setMsgs((m) => [...m, { role: 'user', content: question }]);
    setBusy(true);
    try {
      const r = await api('/chat', { method: 'POST', body: { question, history } });
      setMsgs((m) => [...m, { role: 'assistant', content: r.answer, reasoning: r.reasoning, toolCalls: r.toolCalls,
        latencyMs: r.latencyMs, usage: r.usage, traceId: r.traceId }]);
    } catch (e) {
      if (e instanceof AuthError) return onAuthError();
      setMsgs((m) => [...m, { role: 'assistant', content: e.message, error: true }]);
    } finally { setBusy(false); }
  }

  return (
    <div className="chat">
      <div className="chat-scroll">
        {msgs.length === 0 && (
          <div className="intro">
            <h2>Ask anything about this deployment</h2>
            <p className="muted">Answers come from <code>projectknowledge.md</code> (the maintained design document, stored in S3) plus
              live, read-only data from CloudWatch, CloudFormation and CodePipeline. Secrets and API keys are never available.</p>
            <div className="suggest">{SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => ask(s)}>{s}</button>)}</div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.error ? 'err' : ''}`}>
            {m.role === 'user' ? <div className="bubble">{m.content}</div> : <Answer m={m} />}
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="thinking">Thinking and fetching data…</div></div>}
        <div ref={end} />
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); ask(); }}>
        <textarea value={input} rows={1} placeholder="e.g. How does the RAG retrieval work?" onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } }} />
        <button className="btn primary" disabled={busy || !input.trim()}>Ask</button>
        {msgs.length > 0 && <button type="button" className="btn ghost small" onClick={() => setMsgs([])}>New chat</button>}
      </form>
    </div>
  );
}
