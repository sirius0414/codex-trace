/** 分区尺寸只用于本地布局；小窗口放宽最小值，避免挤走下方记录及详情。 */
export const DEFAULT_OVERVIEW_HEIGHT = 240;
export function overviewHeightBounds(panelHeight: number, chromeHeight: number) {
  const available = Math.max(0, Math.floor(panelHeight - chromeHeight));
  const reservedWork = Math.min(140, Math.ceil(available / 2));
  const max = Math.max(0, available - reservedWork);
  return { min: Math.min(100, max), max };
}
export function clampOverviewHeight(preferred: number, bounds: { min: number; max: number }) {
  return Math.round(Math.max(bounds.min, Math.min(bounds.max, Number.isFinite(preferred) ? preferred : DEFAULT_OVERVIEW_HEIGHT)));
}
