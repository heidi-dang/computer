import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const src = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, src), 'utf8');

test('Phase 6 provides compiled instruction preview and version-preserving recovery', async () => {
	const api = await read('lib/apis/workspace-os.ts');
	const instructions = await read('lib/components/WorkspaceInstructions.svelte');

	assert.match(api, /previewWorkspaceInstructions/);
	assert.match(api, /'instruction_preview'/);
	assert.match(instructions, /Preview compiled/);
	assert.match(instructions, /compiled_instructions/);
	assert.match(instructions, /Restore as draft/);
	assert.match(instructions, /saveWorkspaceInstructions/);
	assert.doesNotMatch(instructions, /deleteInstruction|rewriteHistory/);
});

test('Phase 7 exposes immutable Environment version lifecycle without returning raw secret values', async () => {
	const api = await read('lib/apis/workspace-os.ts');
	const environment = await read('lib/components/WorkspaceEnvironment.svelte');

	assert.match(api, /getWorkspaceEnvironmentVersions/);
	assert.match(api, /createWorkspaceEnvironmentVersion/);
	assert.match(api, /setWorkspaceEnvironmentActiveVersion/);
	assert.match(environment, /Create immutable version/);
	assert.match(environment, /Credential references JSON/);
	assert.match(environment, /Raw credential-like values are rejected by the backend/);
	assert.doesNotMatch(environment, /password|secret_value/);
});

test('Phase 8 reuses canonical Workspace context and managed Memory APIs', async () => {
	const context = await read('lib/components/WorkspaceContextMemory.svelte');

	assert.match(context, /getWorkspaceContext/);
	assert.match(context, /getMemory/);
	assert.match(context, /updateMemory\('workspace'/);
	assert.match(context, /searchMemory/);
	assert.match(context, /Context diagnostics/);
	assert.doesNotMatch(context, /localStorage|EventSource|setInterval/);
});

test('Phase 9 checkpoint UI is explicitly read-only and does not invent restore semantics', async () => {
	const checkpoints = await read('lib/components/WorkspaceCheckpoints.svelte');

	assert.match(checkpoints, /Read-only execution and memory recovery anchors/);
	assert.match(checkpoints, /restore operation is exposed by the current Workspace OS contract/);
	assert.match(checkpoints, /taskFilter/);
	assert.match(checkpoints, /stageFilter/);
	assert.doesNotMatch(checkpoints, /restoreCheckpoint|git reset|git checkout/);
});

test('Phase 10 Tasks is backed by WorkspaceTask actions and existing SSE invalidation', async () => {
	const api = await read('lib/apis/workspace-os.ts');
	const tasks = await read('lib/components/WorkspaceTasks.svelte');
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');
	const reducer = await read('lib/stores/workspace-os-state.ts');

	assert.match(api, /'workspace\.task\.changed'/);
	assert.match(api, /createWorkspaceTask/);
	assert.match(api, /getWorkspaceTaskSummary/);
	assert.match(api, /pinWorkspaceTaskRepository/);
	assert.match(api, /addWorkspaceTaskEvidence/);
	assert.match(tasks, /Durable coordination across repository revisions/);
	assert.match(tasks, /Pin current revision/);
	assert.match(tasks, /repository\.current_revision \?\? repository\.head_revision/);
	assert.match(tasks, /Record evidence/);
	assert.match(tasks, /verification\.verified/);
	assert.match(modal, /\{ id: 'tasks', label: 'Tasks' \}/);
	assert.match(modal, /<WorkspaceTasks/);
	assert.match(reducer, /case 'workspace\.task\.changed':\s*return \['projection', 'tasks'\]/s);
});

test('Phases 6-10 are lazy Workspace Center surfaces rather than startup fan-out', async () => {
	const modal = await read('lib/components/WorkspaceSettingsModal.svelte');

	assert.match(modal, /case 'instructions'/);
	assert.match(modal, /case 'environment'/);
	assert.match(modal, /case 'memory'/);
	assert.match(modal, /case 'checkpoints'/);
	assert.match(modal, /case 'tasks'/);
	assert.match(modal, /let loadedTabs = \$state<Set<TabId>>\(new Set\(\['general'\]\)\)/);
	assert.doesNotMatch(modal, /async function refreshAll/);
});
