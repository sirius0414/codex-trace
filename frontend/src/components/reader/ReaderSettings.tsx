/** 全局设置与展开阅读共用显示偏好；正文及条目内状态不进入持久存储。 */
import { usePreferences } from '../../preferences';
import { readerFontSizes } from '../../ui-preferences';

export function ReaderSettings() {
  const { t, readerFontSize, readerWrap, readerLineNumbers, setReaderFontSize, setReaderWrap, setReaderLineNumbers } = usePreferences();
  return <fieldset className="cx-timeline-settings cx-reader-settings-fields">
    <legend>{t('Preview 阅读', 'Preview reading')}</legend>
    <label className="cx-language-field"><span>{t('正文字号', 'Body font size')}</span><select value={readerFontSize} onChange={event => setReaderFontSize(Number(event.target.value))}>{readerFontSizes.map(size => <option key={size} value={size}>{size} px</option>)}</select></label>
    <label className="cx-setting-toggle"><strong>{t('自动换行', 'Wrap lines')}</strong><input type="checkbox" checked={readerWrap} onChange={event => setReaderWrap(event.target.checked)} /></label>
    <label className="cx-setting-toggle"><strong>{t('显示代码行号', 'Show code line numbers')}</strong><input type="checkbox" checked={readerLineNumbers} onChange={event => setReaderLineNumbers(event.target.checked)} /></label>
  </fieldset>;
}
