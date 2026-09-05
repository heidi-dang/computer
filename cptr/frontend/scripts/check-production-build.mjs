import { spawnSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
const cwd = fileURLToPath(new URL('..', import.meta.url));
const result = spawnSync(npm, ['run', 'build'], {
	cwd,
	encoding: 'utf8',
	env: process.env
});

const output = `${result.stdout ?? ''}${result.stderr ?? ''}`;
process.stdout.write(output);

const cleanOutput = output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
const forbiddenWarnings = [
	/\[vite-plugin-svelte\]/,
	/INEFFECTIVE_DYNAMIC_IMPORT/,
	/Some chunks are larger than/,
	/\[PLUGIN_TIMINGS\]/
];

const clientRoot = path.join(cwd, '.svelte-kit', 'output', 'client');
const manifestPath = path.join(clientRoot, '.vite', 'manifest.json');
const maxEntryBytes = 900 * 1024;
const maxClientChunkBytes = 1400 * 1024;
const maxRootLayoutStaticBytes = 1000 * 1024;
const maxMcpInitialStaticBytes = 900 * 1024;
const maxHomeInitialStaticBytes = 1200 * 1024;
const bundleViolations = [];

try {
	const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
	const seenFiles = new Set();

	for (const [source, record] of Object.entries(manifest)) {
		if (!record || typeof record !== 'object' || typeof record.file !== 'string') continue;
		if (!record.file.endsWith('.js') || seenFiles.has(record.file)) continue;
		seenFiles.add(record.file);

		const bytes = statSync(path.join(clientRoot, record.file)).size;
		const isEntry = record.isEntry === true;
		const limit = isEntry ? maxEntryBytes : maxClientChunkBytes;
		if (bytes > limit) {
			bundleViolations.push(
				`${source}: ${(bytes / 1024).toFixed(1)} kB exceeds ${isEntry ? 'entry' : 'client chunk'} limit ${(limit / 1024).toFixed(0)} kB`
			);
		}
	}

	function findRouteEntry(routeSource) {
		return Object.entries(manifest).find(([source]) => {
			if (!source.startsWith('.svelte-kit/generated/client-optimized/nodes/')) return false;
			try {
				return readFileSync(path.join(cwd, source), 'utf8').includes(routeSource);
			} catch {
				return false;
			}
		});
	}

	function staticEntryClosureBytes(entryKey) {
		const pending = [entryKey];
		const visited = new Set();
		let bytes = 0;
		while (pending.length) {
			const key = pending.pop();
			if (!key || visited.has(key)) continue;
			visited.add(key);
			const record = manifest[key];
			if (!record || typeof record !== 'object') continue;
			if (typeof record.file === 'string' && record.file.endsWith('.js')) {
				bytes += statSync(path.join(clientRoot, record.file)).size;
			}
			if (Array.isArray(record.imports)) pending.push(...record.imports);
		}
		return bytes;
	}

	for (const [label, routeSource, limit] of [
		['Root layout', 'src/routes/+layout.svelte', maxRootLayoutStaticBytes],
		['MCP', 'src/routes/mcp/+page.svelte', maxMcpInitialStaticBytes],
		['Home', 'src/routes/+page.svelte', maxHomeInitialStaticBytes]
	]) {
		const routeEntry = findRouteEntry(routeSource);
		if (!routeEntry) {
			bundleViolations.push(`Unable to identify the ${label} route entry for bundle verification`);
			continue;
		}
		const initialStaticBytes = staticEntryClosureBytes(routeEntry[0]);
		if (initialStaticBytes > limit) {
			bundleViolations.push(
				`${label} entry static JavaScript ${(initialStaticBytes / 1024).toFixed(1)} kB exceeds ${(limit / 1024).toFixed(0)} kB budget`
			);
		}
	}
} catch (error) {
	bundleViolations.push(`Unable to verify client bundle manifest: ${String(error)}`);
}

if (bundleViolations.length) {
	for (const violation of bundleViolations) process.stderr.write(`BUNDLE_LIMIT ${violation}\n`);
}

if (
	(result.status ?? 1) !== 0 ||
	forbiddenWarnings.some((pattern) => pattern.test(cleanOutput)) ||
	bundleViolations.length > 0
) {
	process.exit(1);
}
