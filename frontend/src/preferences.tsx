import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { readUIPreferences, saveUIPreferences, type Language, type UIPreferences, type UIStyle } from './ui-preferences';

/** 为界面组件共享偏好；切换只触发显示更新，不重建观察会话或修改宿主明暗主题。 */
type PreferencesContextValue = UIPreferences & {
  locale: 'zh-CN' | 'en-US';
  setLanguage: (language: Language) => void;
  setUiStyle: (uiStyle: UIStyle) => void;
  setShowSpanSummary: (showSpanSummary: boolean) => void;
  setShowTimelineCursors: (showTimelineCursors: boolean) => void;
  setReaderFontSize: (readerFontSize: number) => void;
  setReaderWrap: (readerWrap: boolean) => void;
  setReaderLineNumbers: (readerLineNumbers: boolean) => void;
  t: (zh: string, en: string) => string;
};
const PreferencesContext = createContext<PreferencesContextValue | null>(null);
function localStorageOrNull() {
  try { return window.localStorage; } catch { return null; }
}

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState(() => readUIPreferences(localStorageOrNull(), navigator.languages?.length ? navigator.languages : [navigator.language]));
  useEffect(() => { saveUIPreferences(localStorageOrNull(), preferences); }, [preferences]);
  useEffect(() => { document.documentElement.lang = preferences.language === 'zh' ? 'zh-CN' : 'en'; }, [preferences.language]);
  const value = useMemo<PreferencesContextValue>(() => ({
    ...preferences,
    locale: preferences.language === 'zh' ? 'zh-CN' : 'en-US',
    setLanguage: language => setPreferences(current => ({ ...current, language })),
    setUiStyle: uiStyle => setPreferences(current => ({ ...current, uiStyle })),
    setShowSpanSummary: showSpanSummary => setPreferences(current => ({ ...current, showSpanSummary })),
    setShowTimelineCursors: showTimelineCursors => setPreferences(current => ({ ...current, showTimelineCursors })),
    setReaderFontSize: readerFontSize => setPreferences(current => ({ ...current, readerFontSize })),
    setReaderWrap: readerWrap => setPreferences(current => ({ ...current, readerWrap })),
    setReaderLineNumbers: readerLineNumbers => setPreferences(current => ({ ...current, readerLineNumbers })),
    t: (zh, en) => preferences.language === 'zh' ? zh : en,
  }), [preferences]);
  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences() {
  const preferences = useContext(PreferencesContext);
  if (!preferences) throw new Error('PreferencesProvider is required.');
  return preferences;
}
