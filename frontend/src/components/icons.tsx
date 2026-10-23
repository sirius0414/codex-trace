type IconName = 'pause' | 'play' | 'down' | 'right' | 'up' | 'download' | 'close' | 'search' | 'alert' | 'user' | 'model' | 'tool' | 'context' | 'compaction' | 'branch' | 'copy' | 'info' | 'database' | 'usage' | 'zoomIn' | 'zoomOut' | 'check';
const paths: Record<IconName, string> = {
  pause: 'M7 3H4v18h3z M17 3h3v18h-3z',
  play: 'M6 3l15 9-15 9z', down: 'M5 9l7 7 7-7', right: 'M9 5l7 7-7 7', up: 'M5 15l7-7 7 7',
  download: 'M12 3v12m-5-5 5 5 5-5 M4 15v5h16v-5', close: 'M6 6l12 12M6 18 18 6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  alert: 'M12 8v5m0 3h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  user: 'M20 21v-2a8 8 0 0 0-16 0v2M17 7a5 5 0 1 1-10 0 5 5 0 0 1 10 0',
  model: 'm12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2zM20 2v4m-2-2h4',
  tool: 'm4 5 7 7-7 7m9 0h7', context: 'M5 3h14v18H5zM8 7h8M8 11h8M8 15h5',
  compaction: 'M3 3h18v5H3zM5 8v13h14V8M9 12h6',
  branch: 'M6 3v12a4 4 0 0 0 4 4h2M6 7h8a4 4 0 0 0 4-4M9 19a3 3 0 1 1-6 0 3 3 0 0 1 6 0M21 3a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  copy: 'M9 9h12v12H9zM5 15H3V3h12v2', info: 'M12 11v6m0-10h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  database: 'M21 5c0 2-4 3-9 3S3 7 3 5s4-3 9-3 9 1 9 3zM3 5v14c0 2 4 3 9 3s9-1 9-3V5M3 12c0 2 4 3 9 3s9-1 9-3',
  usage: 'M5 21V10m7 11V3m7 18V7', zoomIn: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0M6 10h8m-4-4v8',
  zoomOut: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0M6 10h8', check: 'm4 12 5 5L20 6',
};
export default function Icon({ name }: { name: IconName }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}
export type { IconName };
