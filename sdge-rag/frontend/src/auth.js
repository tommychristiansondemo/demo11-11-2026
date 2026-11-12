// Minimal Cognito client (no SDK): self-registration, password sign-in,
// token refresh, and optional SAML SSO via the Cognito hosted UI (PKCE).
const cfg = window.APP_CONFIG || {};
const IDP = `https://cognito-idp.${cfg.region}.amazonaws.com/`;
const STORE_KEY = 'sdge-rag-session';

async function cognito(target, body) {
  const r = await fetch(IDP, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-amz-json-1.1',
      'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const type = (data.__type || '').split('#').pop();
    const msg = data.message || type || 'Authentication error';
    const friendly = {
      UsernameExistsException: 'That username is already taken. Try another one.',
      NotAuthorizedException: 'Incorrect username or password.',
      UserNotFoundException: 'Incorrect username or password.',
      InvalidPasswordException: 'Password must be at least 6 characters.',
    }[type];
    throw new Error(friendly || msg.replace(/^PreSignUp failed with error /, ''));
  }
  return data;
}

export function decodeJwt(token) {
  try {
    const b = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(escape(atob(b))));
  } catch {
    return {};
  }
}

function save(s) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(s)); } catch { /* private mode */ }
}
export function loadSession() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || 'null'); } catch { return null; }
}
export function signOut() {
  try { localStorage.removeItem(STORE_KEY); } catch { /* ignore */ }
}

function toSession(result, prev = {}) {
  const idToken = result.IdToken || result.id_token;
  const claims = decodeJwt(idToken);
  return {
    idToken,
    refreshToken: result.RefreshToken || result.refresh_token || prev.refreshToken,
    expiresAt: claims.exp * 1000,
    user: {
      username: claims['cognito:username'],
      role: claims['custom:role'] || 'employee',
      groups: claims['cognito:groups'] || [],
      isAdmin: (claims['cognito:groups'] || []).includes('admins'),
    },
  };
}

export async function signUp({ username, password, role }) {
  const attrs = [{ Name: 'custom:role', Value: role }];
  await cognito('SignUp', { ClientId: cfg.clientId, Username: username, Password: password, UserAttributes: attrs });
}

export async function signIn(username, password) {
  const data = await cognito('InitiateAuth', {
    ClientId: cfg.clientId,
    AuthFlow: 'USER_PASSWORD_AUTH',
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  const s = toSession(data.AuthenticationResult);
  save(s);
  return s;
}

let refreshing = null;
export async function getValidSession() {
  const s = loadSession();
  if (!s) return null;
  if (Date.now() < s.expiresAt - 60_000) return s;
  if (!s.refreshToken) return null;
  refreshing ||= cognito('InitiateAuth', {
    ClientId: cfg.clientId,
    AuthFlow: 'REFRESH_TOKEN_AUTH',
    AuthParameters: { REFRESH_TOKEN: s.refreshToken },
  }).then((d) => { const ns = toSession(d.AuthenticationResult, s); save(ns); return ns; })
    .catch(() => { signOut(); return null; })
    .finally(() => { refreshing = null; });
  return refreshing;
}

// ---- SSO (SAML via Cognito hosted UI, authorization code + PKCE)
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const redirectUri = () => `${window.location.origin}/`;

export async function startSso() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
  sessionStorage.setItem('pkce', verifier);
  const q = new URLSearchParams({
    response_type: 'code', client_id: cfg.clientId, redirect_uri: redirectUri(),
    identity_provider: cfg.ssoProvider, scope: 'openid email profile',
    code_challenge: challenge, code_challenge_method: 'S256',
  });
  window.location.assign(`https://${cfg.cognitoDomain}/oauth2/authorize?${q}`);
}

export async function completeSsoIfReturning() {
  const code = new URLSearchParams(window.location.search).get('code');
  if (!code) return null;
  window.history.replaceState({}, '', '/');
  const r = await fetch(`https://${cfg.cognitoDomain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code', client_id: cfg.clientId, code,
      redirect_uri: redirectUri(), code_verifier: sessionStorage.getItem('pkce') || '',
    }),
  });
  if (!r.ok) throw new Error('SSO sign-in failed');
  const s = toSession(await r.json());
  save(s);
  return s;
}
