import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');

test('NightOwl design tokens define layered surfaces and accent states', async () => {
  const css = await read('app.css');
  for (const token of [
    '--app-surface',
    '--app-surface-raised',
    '--app-accent',
    '--app-accent-soft',
    '--app-focus-ring',
    '--app-shadow-color'
  ]) {
    assert.match(css, new RegExp(token.replaceAll('-', '\\-')));
  }
  assert.match(css, /\.dark\s*\{/);
  assert.match(css, /#011627/i);
  assert.match(css, /#82aaff/i);
});

test('global UI has accessible focus and reduced-motion treatment', async () => {
  const css = await read('app.css');
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
});

test('mobile UI enforces safe areas and touch-friendly controls', async () => {
  const css = await read('app.css');
  assert.match(css, /safe-area-inset-bottom/);
  assert.match(css, /@media\s*\(max-width:\s*767px\)/);
  assert.match(css, /min-height:\s*2\.75rem/);
});

test('appearance screen exposes the NightOwl preset', async () => {
  const appearance = await read('lib/components/Settings/Appearance.svelte');
  const utils = await read('lib/utils/appearance.ts');
  assert.match(appearance, /NightOwl/);
  assert.match(utils, /NIGHTOWL_THEME_CONFIG/);
});
