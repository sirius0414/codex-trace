import type { UsageSnapshot } from '../../../contracts/types';
import { usePreferences } from '../preferences';
import { usagePresentation } from './usage';

export default function UsageBadge({ values, turn = false }: { values?: UsageSnapshot[]; turn?: boolean }) {
  const { language } = usePreferences();
  const usage = usagePresentation(values, language, turn);
  return <span className={`${turn ? 'cx-turn-usage' : 'cx-entry-usage'} ${usage.available ? '' : 'cx-usage-missing'}`} title={usage.title} aria-label={usage.title}>{usage.text}</span>;
}
