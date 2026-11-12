import { getValidSession } from './auth.js';

const cfg = window.APP_CONFIG || {};

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = 'GET', body } = {}) {
  const s = await getValidSession();
  if (!s) throw new ApiError(401, 'Your session has expired. Please sign in again.');
  const r = await fetch(`/api${path}`, {
    method,
    headers: { Authorization: s.idToken, 'x-api-key': cfg.apiKey, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg = r.status === 429 ? 'Too many requests — please wait a moment and try again.'
      : r.status === 504 ? 'The assistant took too long to respond. Please try again.'
      : data.error || data.message || `Request failed (${r.status})`;
    throw new ApiError(r.status, msg);
  }
  return data;
}

export function uploadToS3(url, file, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.setRequestHeader('Content-Type', contentType);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => (xhr.status < 300 ? resolve() : reject(new Error(`Upload failed (${xhr.status})`)));
    xhr.onerror = () => reject(new Error('Upload failed (network error)'));
    xhr.send(file);
  });
}

// Data API returns timestamps like "2026-11-10 18:04:05.123" (UTC) — normalize for Date()
export function parseTs(s) {
  if (!s) return null;
  let t = String(s).replace(' ', 'T');
  if (/[+-]\d\d$/.test(t)) t += ':00';
  else if (!/(Z|[+-]\d\d:\d\d)$/.test(t)) t += 'Z';
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}
