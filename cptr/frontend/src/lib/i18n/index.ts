/**
 * i18n setup using i18next (framework-agnostic, industry standard).
 *
 * Exports reactive `t` and `locale` Svelte stores for use in components.
 * Browser language detection via i18next-browser-languagedetector.
 */

import i18next, { type TFunction } from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { writable, derived, type Readable } from 'svelte/store';
import en from './locales/en.json';

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
	en: { translation: en }
};

type LocaleModule = { default: Record<string, string> };
const localeLoaders: Record<string, () => Promise<LocaleModule>> = {
	de: () => import('./locales/de.json'),
	es: () => import('./locales/es.json'),
	fr: () => import('./locales/fr.json'),
	ja: () => import('./locales/ja.json'),
	ko: () => import('./locales/ko.json'),
	'pt-BR': () => import('./locales/pt-BR.json'),
	ru: () => import('./locales/ru.json'),
	'zh-CN': () => import('./locales/zh-CN.json'),
	'zh-TW': () => import('./locales/zh-TW.json')
};
const localeLoads = new Map<string, Promise<void>>();

function resolveSupportedLocale(lng: string): string {
	if (lng === 'en' || localeLoaders[lng]) return lng;
	const base = lng.split('-')[0];
	return localeLoaders[base] ? base : 'en';
}

function ensureLocale(lng: string): Promise<void> {
	const localeCode = resolveSupportedLocale(lng);
	if (localeCode === 'en' || i18next.hasResourceBundle(localeCode, 'translation')) {
		return Promise.resolve();
	}
	let load = localeLoads.get(localeCode);
	if (!load) {
		const loader = localeLoaders[localeCode];
		if (!loader) return Promise.resolve();
		load = loader()
			.then(({ default: translations }) => {
				i18next.addResourceBundle(localeCode, 'translation', translations, true, true);
			})
			.finally(() => {
				localeLoads.delete(localeCode);
			});
		localeLoads.set(localeCode, load);
	}
	return load;
}

const initialization = i18next.use(LanguageDetector).init({
	resources,
	fallbackLng: 'en',
	supportedLngs: supportedLocales.map(({ code }) => code),
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

async function loadAndChangeLocale(lng: string): Promise<void> {
	const localeCode = resolveSupportedLocale(lng);
	try {
		await ensureLocale(localeCode);
		await i18next.changeLanguage(localeCode);
	} catch (error) {
		console.error(`Failed to load locale ${localeCode}:`, error);
	}
}

void initialization.then(() => {
	const detectedLocale = resolveSupportedLocale(i18next.language ?? 'en');
	if (detectedLocale !== 'en' && !i18next.hasResourceBundle(detectedLocale, 'translation')) {
		void loadAndChangeLocale(detectedLocale);
	}
});

/** Reactive translation function: use as `$t('key')` or `$t('key', { count: 3 })` in templates. */
export const t: Readable<TFunction> = derived<typeof _tick, TFunction>(
	_tick,
	() => i18next.t.bind(i18next) as TFunction
);

/** Change the active locale. Non-English resource bundles are loaded only when selected. */
export function changeLocale(lng: string): void {
	void loadAndChangeLocale(lng);
}

/**
 * Register a new locale bundle at runtime.
 * Useful for dynamically loaded translations.
 */
export function addLocale(lng: string, translations: Record<string, string>): void {
	i18next.addResourceBundle(lng, 'translation', translations, true, true);
}

export { i18next };
