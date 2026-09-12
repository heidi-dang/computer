import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('Phase 21 lazy-loads Workspace Center and provides bounded load recovery UI', async () => {
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');

	assert.doesNotMatch(sidebar, /import WorkspaceSettingsModal from/);
	assert.match(sidebar, /import\('\.\/WorkspaceSettingsModal\.svelte'\)/);
	assert.match(sidebar, /workspaceSettingsLoadError/);
	assert.match(sidebar, /Workspace Center could not load/);
	assert.match(sidebar, /Retry<\/button/);
	assert.match(sidebar, /Loading only the Workspace management bundle/);
	assert.match(sidebar, /motion-reduce:animate-none/);
});

test('Phase 22 internal Workspace navigation prefers durable UUID and preserves legacy path fallback', async () => {
	const routes = await read('lib/utils/workspaceRoute.ts');
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');
	const page = await read('routes/+page.svelte');
	const search = await read('lib/components/SearchModal.svelte');

	assert.match(routes, /WORKSPACE_ID_PARAM = 'workspaceId'/);
	assert.match(routes, /LEGACY_WORKSPACE_PATH_PARAM = 'workspace'/);
	assert.match(routes, /next\.set\(WORKSPACE_ID_PARAM, workspace\.workspace_id\)/);
	assert.match(routes, /next\.delete\(LEGACY_WORKSPACE_PATH_PARAM\)/);
	assert.match(routes, /next\.set\(LEGACY_WORKSPACE_PATH_PARAM, path\)/);
	assert.match(sidebar, /setWorkspaceRouteForPath/);
	assert.match(page, /setWorkspaceRouteForPath/);
	assert.match(search, /setWorkspaceRouteForPath/);
});

test('Phase 22 URL intents map stable Workspace IDs back to canonical execution paths', async () => {
	const page = await read('routes/+page.svelte');

	assert.match(page, /const workspaceId = params\.get\('workspaceId'\)/);
	assert.match(page, /item\.workspace_id === workspaceId/);
	assert.match(page, /params\.get\('workspace'\) \|\|/);
});

test('Phase 23 makes Workspace Center identity and tab deep-linkable independently of active Workspace route', async () => {
	const routes = await read('lib/utils/workspaceRoute.ts');
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(routes, /WORKSPACE_CENTER_ID_PARAM = 'workspaceCenterId'/);
	assert.match(routes, /WORKSPACE_CENTER_TAB_PARAM = 'workspaceCenter'/);
	assert.match(routes, /normalizeWorkspaceCenterTab/);
	assert.match(sidebar, /setWorkspaceCenterRoute\(\$page\.url\.searchParams/);
	assert.match(sidebar, /clearWorkspaceCenterRoute\(\$page\.url\.searchParams\)/);
	assert.match(sidebar, /initialTab=\{workspaceSettingsInitialTab\}/);
	assert.match(sidebar, /ontabchange=\{updateWorkspaceSettingsTab\}/);
	assert.match(modal, /initialTab\?: string/);
	assert.match(modal, /ontabchange\?: \(tab: string\) => void/);
	assert.match(modal, /selectTab\(tab\.id\)/);
});

test('Phase 23 invalid Workspace Center links fail closed after state hydration', async () => {
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');

	assert.match(sidebar, /if \(!target\) \{/);
	assert.match(sidebar, /if \(\$stateLoaded\)/);
	assert.match(sidebar, /clearWorkspaceCenterRoute\(\$page\.url\.searchParams\)/);
	assert.match(sidebar, /requestedTab !== normalizedTab/);
	assert.match(sidebar, /replaceState: true/);
});

test('Phase 24 workspace loads and routes use generation guards against stale async wins', async () => {
	const stores = await read('lib/stores.ts');
	const page = await read('routes/+page.svelte');

	assert.match(stores, /let workspaceLoadGeneration = 0/);
	assert.match(stores, /const generation = \+\+workspaceLoadGeneration/);
	assert.match(stores, /generation !== workspaceLoadGeneration/);
	assert.match(stores, /export function clearCurrentWorkspace/);
	assert.match(stores, /workspaceLoadGeneration \+= 1/);
	assert.match(page, /let workspaceRouteGeneration = 0/);
	assert.match(page, /const generation = \+\+workspaceRouteGeneration/);
	assert.match(page, /generation !== workspaceRouteGeneration/);
	assert.match(page, /loaded\.workspace_id !== resolution\.workspaceId/);
});

test('Phase 24 unknown UUID routes wait for state hydration then fail closed', async () => {
	const page = await read('routes/+page.svelte');

	assert.match(page, /if \(resolution\.unresolvedId\)/);
	assert.match(page, /if \(!\$stateLoaded\) return/);
	assert.match(page, /clearCurrentWorkspace\(\)/);
	assert.match(page, /clearWorkspaceRoute\(params\)/);
});

test('Phase 25 keeps remaining path-first routes limited to compatibility or arbitrary-path surfaces', async () => {
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');
	const search = await read('lib/components/SearchModal.svelte');
	const page = await read('routes/+page.svelte');

	assert.doesNotMatch(sidebar, /goto\(`\/\?workspace=\$\{encodeURIComponent/);
	assert.doesNotMatch(search, /goto\(`\/\?workspace=\$\{encodeURIComponent/);
	assert.doesNotMatch(page, /chatWorkspace\)}&chatId/);
	assert.match(page, /legacy\/external flows may still use/);
});
