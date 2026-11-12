// Auth against the Gas Ops Cognito user pool (same accounts) + API helper.
const cfg = window.APP_CONFIG || {};
const KEY = 'sdge-insights-session';

async function cognito(target, body) {
  const r = await fetch(`https://cognito-idp.${cfg.region}.amazonaws.com/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}` },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const t = (d.__type || '').split('#').pop();
    throw new Error(t === 'NotAuthorizedException' || t === 'UserNotFoundException'
      ? 'Incorrect username or password.' : d.message || 'Sign-in failed');
  }
  return d;
}

function claims(tok) {
  try { return JSON.parse(atob(tok.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))); } catch { return {}; }
}
function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* ignore */ } }
function load() { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { return null; } }
export function signOut() { try { localStorage.removeItem(KEY); } catch { /* ignore */ } }

function toSession(res, prev = {}) {
  const c = claims(res.IdToken);
  return {
    idToken: res.IdToken, refreshToken: res.RefreshToken || prev.refreshToken, expiresAt: c.exp * 1000,
    user: { username: c['cognito:username'], isAdmin: (c['cognito:groups'] || []).includes('admins') },
  };
}

export async function signIn(username, password) {
  const d = await cognito('InitiateAuth', {
    ClientId: cfg.clientId, AuthFlow: 'USER_PASSWORD_AUTH', AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  const s = toSession(d.AuthenticationResult);
  save(s);
  return s;
}

export async function getSession() {
  const s = load();
  if (!s) return null;
  if (Date.now() < s.expiresAt - 60_000) return s;
  try {
    const d = await cognito('InitiateAuth', {
      ClientId: cfg.clientId, AuthFlow: 'REFRESH_TOKEN_AUTH', AuthParameters: { REFRESH_TOKEN: s.refreshToken },
    });
    const ns = toSession(d.AuthenticationResult, s);
    save(ns);
    return ns;
  } catch {
    signOut();
    return null;
  }
}

export class AuthError extends Error {}

export async function api(path, { method = 'GET', body } = {}) {
  const s = await getSession();
  if (!s) throw new AuthError('Session expired');
  const r = await fetch(`/api${path}`, {
    method,
    headers: { Authorization: `Bearer ${s.idToken}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { signOut(); throw new AuthError('Session expired'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || d.message || `Request failed (${r.status})`);
  return d;
}

export const fmtTime = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(); };
