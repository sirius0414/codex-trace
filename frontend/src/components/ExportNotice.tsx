import { useState } from 'react';
import { usePreferences } from '../preferences';
import Icon from './icons';

/** Result text remains readable until dismissed, without covering adjacent controls. */
export default function ExportNotice({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  const { t } = usePreferences();
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  return <section className="cx-export-message" aria-label={t('导出结果', 'Export result')}>
    <div className="cx-export-message-text" role="status" tabIndex={0}>{message}</div>
    <button type="button" className="cx-icon" aria-label={t('关闭导出提示', 'Dismiss export notice')} title={t('关闭导出提示', 'Dismiss export notice')} onClick={() => { setDismissed(true); onDismiss(); }}><Icon name="close" /></button>
  </section>;
}
