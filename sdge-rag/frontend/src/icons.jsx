export const Flame = ({ size = 24 }) => (
  <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" rx="7" fill="var(--brand-deep)" />
    <path d="M16 5c1 4.5 6.5 7.5 6.5 13.2A6.5 6.5 0 0 1 9.5 18.2c0-3 1.7-5 3.2-6.6.3 2 1.3 3.2 2.6 3.7C14.6 11.8 15 8.2 16 5z" fill="#f59e0b" />
    <path d="M16 15.5c.6 2.2 3 3.4 3 6a3 3 0 0 1-6 0c0-1.4.8-2.4 1.6-3.1.2.9.6 1.4 1.1 1.6-.3-1.6-.2-3.2.3-4.5z" fill="#fde68a" />
  </svg>
);

const Icon = ({ d, size = 18 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={d} /></svg>
);
export const Send = (p) => <Icon {...p} d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z" />;
export const Upload = (p) => <Icon {...p} d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />;
export const Trash = (p) => <Icon {...p} d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />;
export const Refresh = (p) => <Icon {...p} d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5" />;
export const FileIcon = (p) => <Icon {...p} d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6" />;
export const Shield = (p) => <Icon {...p} d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />;
