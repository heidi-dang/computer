/**
 * i18n setup using i18next (framework-agnostic, industry standard).
 *
 * Exports reactive `t` and `locale` Svelte stores for use in components.
 * Browser language detection via i18next-browser-languagedetector.
 */

import i18next, { type TFunction } from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { writable, derived } from 'svelte/store';
import en from './locales/en.json';
import de from './locales/de.json';
import es from './locales/es.json';
import fr from './locales/fr.json';
import ja from './locales/ja.json';
import ko from './locales/ko.json';
import ptBR from './locales/pt-BR.json';
import ru from './locales/ru.json';
import zhCN from './locales/zh-CN.json';
import zhTW from './locales/zh-TW.json';

export const supportedLocales = [
	{ code: 'en', label: 'English' },
	// Alphabetical by label below:
	{ code: 'de', label: 'Deutsch' },
	{ code: 'es', label: 'Español' },
	{ code: 'fr', label: 'Français' },
	{ code: 'pt-BR', label: 'Português (Brasil)' },
	{ code: 'ru', label: 'Русский' },
	{ code: 'ja', label: '日本語' },
	{ code: 'ko', label: '한국어' },
	{ code: 'zh-CN', label: '简体中文' },
	{ code: 'zh-TW', label: '繁體中文' }
] as const;

const resources: Record<string, { translation: Record<string, string> }> = {
	en: { translation: en },
	de: { translation: de },
	es: { translation: es },
	fr: { translation: fr },
	ja: { translation: ja },
	ko: { translation: ko },
	'pt-BR': { translation: ptBR },
	ru: { translation: ru },
	'zh-CN': { translation: zhCN },
	'zh-TW': { translation: zhTW }
};

i18next.use(LanguageDetector).init({
	resources,
	fallbackLng: 'en',
	interpolation: {
		escapeValue: false // Svelte handles escaping
	},
	detection: {
		order: ['localStorage', 'navigator'],
		caches: ['localStorage'],
		lookupLocalStorage: 'cptr_locale'
	}
});

// ── Svelte store wrapper ────────────────────────────────────────

/** Writable store tracking the current locale code. */
export const locale = writable<string>(i18next.language ?? 'en');

/**
 * Internal ticker that increments on every language change.
 * Forces the derived `t` store to re-evaluate.
 */
const _tick = writable(0);

i18next.on('languageChanged', (lng: string) => {
	locale.set(lng);
	_tick.update((n) => n + 1);
});

/** Reactive translation function: use as `$t('key')` or `$t('key', { count: 3 })` in templates. */
export const t = derived(_tick, () => i18next.t.bind(i18next) as TFunction);

/** Change the active locale. */
export function changeLocale(lng: string): void {
	i18next.changeLanguage(lng);
}

/**
 * Register a new locale bundle at runtime.
 * Useful for dynamically loaded translations.
 */
export function addLocale(lng: string, translations: Record<string, string>): void {
	i18next.addResourceBundle(lng, 'translation', translations, true, true);
}

export { i18next };
