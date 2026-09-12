import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('Workspace Center defaults to Overview and lazy-loads detail sections', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(modal, /let activeTab = \$state<TabId>\('overview'\)/);
	assert.match(modal, /async function loadTab\(tab: TabId, force = false\)/);
	assert.match(modal, /case 'repositories': \{/);
	assert.match(modal, /getWorkspaceRepositories\(workspace\.workspace_id\)/);
	assert.match(modal, /case 'instructions': \{/);
	assert.match(modal, /getWorkspaceInstructionHistory\(workspace\.workspace_id\)/);
	assert.match(modal, /case 'environment': \{/);
	assert.match(modal, /case 'memory':/);
	assert.match(modal, /case 'checkpoints': \{/);
	assert.match(modal, /case 'activity':/);
	assert.match(modal, /case 'health':/);
	assert.doesNotMatch(modal, /async function refreshAll/);
});

test('non-active Workspace Center uses projection snapshot without replacing the active SSE store', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(
		modal,
		/isLiveTarget = \$derived\(workspaceOsStore\.workspaceId === workspace\.workspace_id\)/
	);
	assert.match(
		modal,
		/snapshotProjection = await getWorkspaceProjection\(workspace\.workspace_id\)/
	);
	assert.match(modal, /if \(isLiveTarget\) \{\s*await workspaceOsStore\.refresh\(\)/s);
	assert.doesNotMatch(modal, /workspaceOsStore\.connect\(workspace\.workspace_id\)/);
	assert.doesNotMatch(modal, /workspaceOsStore\.disconnect\(\)/);
});

test('Workspace Center renders backend projection truth and active workspace live status', async () => {
	const overview = await read('lib/components/WorkspaceOverview.svelte');
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');

	assert.match(overview, /projection\.health\.checks\.repositories_healthy/);
	assert.match(overview, /projection\.workbench\.active_sessions_count/);
	assert.match(overview, /projection\.context_cache\.is_cached/);
	assert.match(overview, /projection\.context_cache\.fingerprint/);
	assert.doesNotMatch(overview, /projection\.fingerprint/);
	assert.match(overview, /projection\.recent_events/);
	assert.match(overview, /projection\.metrics\.hit_ratio/);

	assert.match(sidebar, /workspaceOsStore\.connection\.status/);
	assert.match(sidebar, /workspace-live-indicator/);
	assert.match(sidebar, /label: 'Workspace Center'/);
});

test('Activity and Health surfaces consume the existing projection and health contracts', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const activity = await read('lib/components/WorkspaceActivity.svelte');
	const health = await read('lib/components/WorkspaceHealth.svelte');

	assert.match(modal, /<WorkspaceActivity/);
	assert.match(modal, /<WorkspaceHealth/);
	assert.match(activity, /projection\.recent_events/);
	assert.match(activity, /event\.sequence/);
	assert.match(activity, /filter === 'all'/);
	assert.doesNotMatch(activity, /setInterval|EventSource|fetch\(/);
	assert.match(health, /health\?\.health_band/);
	assert.match(health, /summary\.stale_with_changes/);
	assert.match(health, /health\?\.diagnostics/);
	assert.match(health, /Advanced health details/);
	assert.doesNotMatch(health, /setInterval|EventSource|fetch\(/);
});

test('successful mutations refresh the Overview without making mutation success depend on it', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(modal, /function refreshOverviewAfterMutation\(\)/);
	assert.match(modal, /refreshProjectionSummary\(\)\s*\.then\(\(\) => markLoaded\('overview'\)\)/s);
	assert.match(modal, /refreshOverviewAfterMutation\(\);\s*toast\.success\('Workspace updated'\)/s);
	assert.match(modal, /refreshOverviewAfterMutation\(\);\s*toast\.success\('Repository added'\)/s);
	assert.match(
		modal,
		/refreshOverviewAfterMutation\(\);\s*toast\.success\('Workspace instructions saved'\)/s
	);
	assert.match(
		modal,
		/refreshOverviewAfterMutation\(\);\s*toast\.success\('Environment profile created'\)/s
	);
});

test('lazy detail refresh clears only the stale domains that were reloaded', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const store = await read('lib/stores/workspace-os.svelte.ts');

	assert.match(modal, /function domainsForTab\(tab: TabId\)/);
	assert.match(modal, /isTabStale\(tab\)/);
	assert.match(modal, /workspaceOsStore\.markFresh\(domainsForTab\(tab\)\)/);
	assert.match(store, /markFresh\(domains: readonly WorkspaceOsDomain\[\]\)/);
	assert.match(store, /markWorkspaceDomainsFresh\(_state, domains\)/);
});
