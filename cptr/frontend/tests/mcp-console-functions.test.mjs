import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../src/', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');
const utilitiesUrl = new URL('../src/lib/utils/mcp-console.ts', import.meta.url);

test('server list loads, renders health, caches tools, refreshes safely, reconnects, selects tools, and keeps Admin links', async () => {
	const [serverList, console] = await Promise.all([
		read('lib/components/mcp/McpServerList.svelte'),
		read('lib/components/mcp/McpConsole.svelte')
	]);

	assert.match(serverList, /listMcpServers/);
	assert.match(serverList, /server\.health === 'connected'/);
	assert.match(serverList, /server\.health === 'timeout'/);
	assert.match(serverList, /listServerTools\(server\.id\)/);
	assert.match(serverList, /if \(toolsByServer\[server\.id\]\) return/);
	assert.match(serverList, /ensureServerExpanded/);
	assert.match(serverList, /onclick=\{\(\) => (?:void )?loadServers\(false\)\}/);
	assert.match(serverList, /reconnectMcpServer\(server\.id\)/);
	assert.match(serverList, /const wasExpanded = expandedServers\.has\(server\.id\)/);
	assert.match(serverList, /delete toolsByServer\[server\.id\]/);
	assert.match(serverList, /await loadServers\(false\)/);
	assert.match(serverList, /if \(wasExpanded\)[\s\S]{0,250}ensureServerExpanded/);
	assert.match(serverList, /onSelectTool\(serverId, tool\)/);
	assert.match(console, /mobileView = 'tool'/);
	assert.ok((serverList.match(/href="\/admin"/g) ?? []).length >= 2);
});

test('tool argument coercion produces enum, boolean, number, object, array, and string values and preserves them for raw JSON', async () => {
	const { coerceMcpToolArguments, formatMcpToolArguments } = await import(utilitiesUrl);
	const properties = {
		mode: { type: 'string', enum: ['fast', 'safe'] },
		enabled: { type: 'boolean' },
		count: { type: 'integer' },
		ratio: { type: 'number' },
		options: { type: 'object' },
		items: { type: 'array' },
		label: { type: 'string' }
	};
	const formValues = {
		mode: 'safe',
		enabled: 'false',
		count: '42',
		ratio: '1.5',
		options: '{"retries":2}',
		items: '["a","b"]',
		label: 'hello'
	};
	const result = coerceMcpToolArguments({
		rawJsonMode: false,
		rawJson: '{}',
		formValues,
		properties,
		required: Object.keys(properties)
	});

	assert.equal(result.error, null);
	assert.deepEqual(result.args, {
		mode: 'safe',
		enabled: false,
		count: 42,
		ratio: 1.5,
		options: { retries: 2 },
		items: ['a', 'b'],
		label: 'hello'
	});
	assert.deepEqual(JSON.parse(formatMcpToolArguments(result.args)), result.args);
});

test('invalid raw or structured JSON blocks invocation with a visible error contract', async () => {
	const { coerceMcpToolArguments } = await import(utilitiesUrl);
	const invalidRaw = coerceMcpToolArguments({
		rawJsonMode: true,
		rawJson: '{bad json',
		formValues: {},
		properties: {},
		required: []
	});
	assert.equal(invalidRaw.args, null);
	assert.match(invalidRaw.error ?? '', /JSON/i);

	const nonObjectRaw = coerceMcpToolArguments({
		rawJsonMode: true,
		rawJson: '[]',
		formValues: {},
		properties: {},
		required: []
	});
	assert.equal(nonObjectRaw.args, null);
	assert.match(nonObjectRaw.error ?? '', /object/i);

	const invalidStructured = coerceMcpToolArguments({
		rawJsonMode: false,
		rawJson: '{}',
		formValues: { options: '{bad' },
		properties: { options: { type: 'object' } },
		required: ['options']
	});
	assert.equal(invalidStructured.args, null);
	assert.match(invalidStructured.error ?? '', /options/i);

	const toolForm = await read('lib/components/mcp/McpToolForm.svelte');
	assert.match(toolForm, /jsonError/);
	assert.match(toolForm, /\{#if jsonError\}/);
});

test('Invoke and Ctrl or Cmd Enter share one submit path and invoke exactly once', async () => {
	const toolForm = await read('lib/components/mcp/McpToolForm.svelte');
	assert.match(toolForm, /function submitCurrent/);
	assert.match(toolForm, /function handleSubmit[\s\S]{0,220}submitCurrent\(\)/);
	assert.match(toolForm, /function handleKeydown[\s\S]{0,260}submitCurrent\(\)/);
	assert.match(toolForm, /e\.preventDefault\(\)/);
	assert.equal((toolForm.match(/onInvoke\(/g) ?? []).length, 1);
});

test('streaming Console invocation appends chunks, completes, fails with toast, and moves to Activity on invoke', async () => {
	const console = await read('lib/components/mcp/McpConsole.svelte');
	assert.match(console, /invokeToolStreaming\(serverId, toolName, args/);
	assert.match(console, /onChunk\(item: McpContentItem\)/);
	assert.match(console, /contentItems:\s*\[\.\.\.\(current\.contentItems \?\? \[\]\), item\]/);
	assert.match(console, /onDone\(result: McpContentItem\[\]\)/);
	assert.match(console, /phase: 'complete'/);
	assert.match(console, /onError\(message: string\)/);
	assert.match(console, /phase: 'failed'/);
	assert.match(console, /toast\.error/);
	assert.match(console, /mobileView = 'console'/);
	assert.match(console, />\s*Servers\s*</);
	assert.match(console, />\s*Activity\s*</);
	assert.match(console, />\s*Tool\s*</);
});

test('SSE parser retains partial frames and flushes a final frame without a trailing blank line', async () => {
	const { consumeMcpSseBuffer } = await import(utilitiesUrl);
	const first = consumeMcpSseBuffer(
		'event: tool_chunk\r\ndata: {"type":"text","text":"hello"}\r\n\r\nevent: tool_done\ndata: {"result":[]}',
		false
	);
	assert.deepEqual(first.frames, [{ event: 'tool_chunk', data: { type: 'text', text: 'hello' } }]);
	assert.match(first.remainder, /tool_done/);

	const final = consumeMcpSseBuffer(first.remainder, true);
	assert.deepEqual(final.frames, [{ event: 'tool_done', data: { result: [] } }]);
	assert.equal(final.remainder, '');

	const api = await read('lib/apis/mcp.ts');
	assert.match(api, /consumeMcpSseBuffer/);
	assert.match(api, /consumeMcpSseBuffer\(buffer, true\)/);
});

test('Activity reconnect resnapshots before reopening, Clear is presentation-only, and Refresh restores history', async () => {
	const feed = await read('lib/components/mcp/McpActivityFeed.svelte');
	const snapshotIndex = feed.indexOf('applySnapshot(await getMcpActivitySnapshot())');
	const streamIndex = feed.indexOf('closeStream = openMcpActivityStream');
	assert.ok(snapshotIndex >= 0 && streamIndex > snapshotIndex);
	assert.match(feed, /1000,\s*2000,\s*4000,\s*8000/);
	assert.match(feed, /hiddenBeforeSequence = state\?\.sequence \?\? hiddenBeforeSequence/);
	assert.match(feed, /onClearConsole\?\.\(\)/);
	assert.match(feed, /if \(restoreHistory\) hiddenBeforeSequence = 0/);
	assert.match(feed, /refreshAndOpen\(true\)/);
});

test('activity cards render text, image, resource, errors, and keyboard expand or collapse', async () => {
	const card = await read('lib/components/mcp/McpCallCard.svelte');
	assert.match(card, /item\.type === 'text'/);
	assert.match(card, /item\.type === 'image'/);
	assert.match(card, /item\.type === 'resource'/);
	assert.match(card, /record\.errorJson/);
	assert.match(card, /event\.key === 'Enter' \|\| event\.key === ' '/);
	assert.match(card, /event\.preventDefault\(\)/);
	assert.match(card, /toggleExpanded\(\)/);
	assert.match(card, /aria-expanded=\{expanded\}/);
});
