import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('Phase 5 exposes typed owner-scoped Workspace Group REST contracts', async () => {
	const api = await read('lib/apis/workspace-os.ts');

	assert.match(api, /export interface WorkspaceGroup \{/);
	assert.match(api, /export interface WorkspaceGroupMember \{/);
	assert.match(api, /export const listWorkspaceGroups/);
	assert.match(
		api,
		/fetchJSON<\{ groups: WorkspaceGroup\[\]; total: number \}>\('\/api\/workspace-groups'\)/
	);
	assert.match(api, /export const createWorkspaceGroup/);
	assert.match(api, /method: 'POST'/);
	assert.match(api, /export const updateWorkspaceGroupMember/);
	assert.match(api, /members\/\$\{encodeURIComponent\(workspaceId\)\}/);
	assert.match(api, /export const reorderWorkspaceGroupMembers/);
	assert.match(api, /method: 'PUT'/);
	assert.match(api, /export const deleteWorkspaceGroup/);
});

test('Workspace Center Phase 5 adds Groups and repository topology without replacing existing repository controls', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(modal, /\{ id: 'groups', label: 'Groups' \}/);
	assert.match(modal, /<WorkspaceRepositoryTopology workspaceName=\{name\} \{repositories\} \/>/);
	assert.match(
		modal,
		/<WorkspaceGroups workspaceId=\{workspace\.workspace_id\} workspaceName=\{name\} \/>/
	);
	assert.match(modal, /addWorkspaceRepository\(workspace\.workspace_id/);
	assert.match(modal, /updateWorkspaceRepository\(workspace\.workspace_id/);
	assert.match(modal, /removeWorkspaceRepository\(workspace\.workspace_id/);
});

test('repository topology is a pure projection of canonical repository and checkout state', async () => {
	const topology = await read('lib/components/WorkspaceRepositoryTopology.svelte');

	assert.match(topology, /repositories\.filter\(\(repository\) => repository\.enabled\)/);
	assert.match(topology, /repository\.canonical_remote_identity/);
	assert.match(topology, /checkout\.canonical/);
	assert.match(topology, /checkout\.last_seen_revision/);
	assert.match(topology, /checkout\.available/);
	assert.doesNotMatch(topology, /fetch\(|EventSource|setInterval|setTimeout/);
});

test('Workspace Groups uses explicit refresh and confirmation-protected destructive mutations', async () => {
	const groups = await read('lib/components/WorkspaceGroups.svelte');

	assert.match(groups, /listWorkspaceGroups\(\)/);
	assert.match(groups, /getWorkspaceList\(\)/);
	assert.match(groups, /createWorkspaceGroup\(/);
	assert.match(
		groups,
		/members: \[\{ workspace_id: workspaceId, role: 'member', primary: true \}\]/
	);
	assert.match(groups, /requestConfirm\(\{/);
	assert.match(groups, /deleteWorkspaceGroup\(group\.id\)/);
	assert.match(groups, /removeWorkspaceGroupMember\(group\.id, member\.workspace_id\)/);
	assert.match(groups, /updateWorkspaceGroupMember\(group\.id, member\.workspace_id/);
	assert.match(groups, /reorderWorkspaceGroupMembers/);
	assert.doesNotMatch(groups, /EventSource|setInterval/);
});

test('Groups deliberately does not claim Workspace SSE invalidation coverage', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const state = await read('lib/stores/workspace-os-state.ts');

	assert.match(modal, /case 'groups':\s*return \[\]/s);
	assert.doesNotMatch(state, /'groups'/);
});
