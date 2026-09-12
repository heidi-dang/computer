import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('Phase 11 exposes owner-scoped Workbench sessions without a second live-state transport', async () => {
	const api = await read('lib/apis/workbench.ts');
	const workbench = await read('lib/components/WorkspaceWorkbench.svelte');
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(api, /\/api\/control\/v1\/workbench-sessions/);
	assert.match(api, /getWorkbenchSessionEvents/);
	assert.match(api, /archiveWorkbenchSession/);
	assert.match(
		workbench,
		/session\.workspace_id === workspaceId \|\| session\.active_workspace_id === workspaceId/
	);
	assert.match(workbench, /getWorkbenchSessionEvents\(sessionId, 0, 200\)/);
	assert.match(workbench, /generation !== eventGeneration \|\| selectedSessionId !== sessionId/);
	assert.doesNotMatch(workbench, /EventSource|setInterval/);
	assert.match(modal, /\{ id: 'workbench', label: 'Workbench' \}/);
	assert.match(modal, /<WorkspaceWorkbench/);
});

test('Phase 12 ADMIN is temporary Workbench authority and never masquerades as ROOT', async () => {
	const api = await read('lib/apis/workbench.ts');
	const admin = await read('lib/components/WorkspaceAdminPrivilege.svelte');
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(api, /admin-grant/);
	assert.match(api, /admin-revoke/);
	assert.match(api, /\/privilege/);
	assert.match(admin, /Math\.max\(1, Math\.min\(480/);
	assert.match(admin, /This is session-scoped and does not grant ROOT/);
	assert.match(admin, /ROOT remains a separate prompt-scoped\/local-root[\s\S]*grant/);
	assert.match(
		admin,
		/ADMIN does not bypass workspace ownership, API scopes, guard controls or command policy/
	);
	assert.match(admin, /generation !== privilegeGeneration \|\| selectedSessionId !== sessionId/);
	assert.match(modal, /\{ id: 'admin', label: 'Admin' \}/);
	assert.match(modal, /<WorkspaceAdminPrivilege/);
});

test('Phase 13 new-chat UX uses explicit stable Workspace identity and fails closed on mismatch', async () => {
	const chatApi = await read('lib/apis/chat.ts');
	const chat = await read('lib/components/chat/ChatPanel.svelte');
	const page = await read('routes/+page.svelte');

	assert.match(chatApi, /workspace_id: string \| null/);
	assert.match(chat, /workspaceId\?: string/);
	assert.match(chat, /data\.chat\.workspace_id !== workspaceId/);
	assert.match(chat, /Workspace binding mismatch/);
	assert.match(chat, /if \(workspaceBindingError\)/);
	assert.match(chat, /files,\s*workspaceId/s);
	assert.doesNotMatch(chat, /get\(currentWorkspace\)\?\.workspace_id/);
	assert.match(chat, /workspace-binding-chip/);
	assert.match(page, /workspaceId=\{\$currentWorkspace!\.workspace_id\}/);
});

test('Phase 14 Workspace presence enriches only the active Workspace without N-workspace polling', async () => {
	const sidebar = await read('lib/components/SidebarWorkspaceList.svelte');

	assert.match(sidebar, /liveWorkspaceProjection = \$derived\(workspaceOsStore\.projection\)/);
	assert.match(sidebar, /health \$\{health\}/);
	assert.match(sidebar, /active Workbench session/);
	assert.match(sidebar, /workspace-workbench-count/);
	assert.doesNotMatch(sidebar, /setInterval\([^)]*Workspace|EventSource/);
});

test('Phase 15 Activity and Health expose recovery state and canonical health checks', async () => {
	const activity = await read('lib/components/WorkspaceActivity.svelte');
	const health = await read('lib/components/WorkspaceHealth.svelte');

	assert.match(activity, /Sequence replay will recover missed durable events/);
	assert.match(activity, /Retry sync/);
	assert.match(activity, /data-status=\{connectionStatus\}/);
	assert.match(health, /projectionChecks/);
	assert.match(health, /Path configured/);
	assert.match(health, /Repositories healthy/);
	assert.match(health, /Workbench sessions/);
	assert.match(health, /Needs attention/);
});

test('Phase 16 async detail surfaces use generation guards instead of stale request wins', async () => {
	const workbench = await read('lib/components/WorkspaceWorkbench.svelte');
	const admin = await read('lib/components/WorkspaceAdminPrivilege.svelte');
	const tasks = await read('lib/components/WorkspaceTasks.svelte');
	const memory = await read('lib/components/WorkspaceContextMemory.svelte');

	assert.match(workbench, /loadGeneration/);
	assert.match(workbench, /eventGeneration/);
	assert.match(workbench, /onDestroy/);
	assert.match(admin, /loadGeneration/);
	assert.match(admin, /privilegeGeneration/);
	assert.match(tasks, /summaryGeneration/);
	assert.match(tasks, /selectedTaskId !== taskId/);
	assert.match(memory, /searchGeneration/);
	assert.match(memory, /searchQuery\.trim\(\) !== query/);
	assert.match(workbench, /attemptedStaleRefresh/);
	assert.match(admin, /attemptedStaleRefresh/);
	assert.match(tasks, /attemptedStaleRefresh/);
});

test('Phase 17 Workspace Center implements semantic keyboard tabs and accessible errors', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const workbench = await read('lib/components/WorkspaceWorkbench.svelte');
	const admin = await read('lib/components/WorkspaceAdminPrivilege.svelte');

	assert.match(modal, /role="tablist"/);
	assert.match(modal, /role="tab"/);
	assert.match(modal, /aria-selected=\{activeTab === tab\.id\}/);
	assert.match(modal, /role="tabpanel"/);
	assert.match(modal, /ArrowDown/);
	assert.match(modal, /ArrowLeft/);
	assert.match(modal, /event\.key === 'Home'/);
	assert.match(modal, /event\.key === 'End'/);
	assert.match(modal, /min-h-11 min-w-11/);
	assert.match(workbench, /role="alert"/);
	assert.match(admin, /role="alert"/);
});

test('Phase 18 touched Workspace surfaces include responsive and reduced-motion behavior', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const workbench = await read('lib/components/WorkspaceWorkbench.svelte');
	const admin = await read('lib/components/WorkspaceAdminPrivilege.svelte');
	const chat = await read('lib/components/chat/ChatPanel.svelte');

	assert.match(modal, /p-3 sm:p-5/);
	assert.match(modal, /overflow-x-auto overscroll-contain/);
	assert.match(modal, /prefers-reduced-motion/);
	assert.match(workbench, /@media \(max-width: 820px\)/);
	assert.match(workbench, /@media \(max-width: 540px\)/);
	assert.match(workbench, /prefers-reduced-motion/);
	assert.match(admin, /@media \(max-width: 760px\)/);
	assert.match(admin, /prefers-reduced-motion/);
	assert.match(chat, /mobile-safe-bottom/);
});

test('Phase 19 keeps Phases 11-18 lazy within Workspace Center', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(modal, /case 'workbench':/);
	assert.match(modal, /case 'admin':/);
	assert.match(modal, /!\['tasks', 'workbench', 'admin'\]\.includes\(tab\)/);
	assert.doesNotMatch(modal, /async function refreshAll/);
	assert.doesNotMatch(
		modal,
		/Promise\.all\([\s\S]*listWorkbenchSessions[\s\S]*getWorkbenchPrivilege/
	);
});
