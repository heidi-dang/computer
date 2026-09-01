import { spawnSync } from 'node:child_process';
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

if ((result.status ?? 1) !== 0 || forbiddenWarnings.some((pattern) => pattern.test(cleanOutput))) {
	process.exit(1);
}
