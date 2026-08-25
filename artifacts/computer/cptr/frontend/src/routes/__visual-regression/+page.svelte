<script lang="ts">
	import Tools from '$lib/components/Admin/Tools.svelte';
	import ToolServers from '$lib/components/Admin/ToolServers.svelte';
	import Terminal from '$lib/components/Terminal.svelte';
	import ToolCallCollapsible from '$lib/components/chat/ToolCallCollapsible.svelte';
	import DesignerResults from '$lib/components/chat/DesignerResults.svelte';
import LiveTerminal from '$lib/components/chat/LiveTerminal.svelte';
	import { designerVisualFixture } from '$lib/components/chat/designer-fixtures';

	const toolCall = {
		name: 'run_command',
		call_id: 'visual-call',
		status: 'completed',
		arguments: {
			command:
				'node --max-old-space-size=4096 scripts/build-with-a-deliberately-long-command-name.js --workspace ./artifacts/computer'
		}
	};

	const toolOutput = {
		output: JSON.stringify({
			status: 'completed',
			summary: 'Build finished successfully',
			files: ['dist/index.html', 'dist/assets/app.js']
		})
	};

const liveTerminalEvents = [
{ sequence: 1, kind: 'HEIDI_VALIDATION_PASSED', payload: { summary: 'qualified CPTR path' } },
{ sequence: 2, kind: 'SPECIALIST_DISPATCHED', payload: { specialist_id: 'build-agent', attempt_id: 'attempt-42' } },
{ sequence: 3, kind: 'TOOL_STARTED', payload: { tool_name: 'run_command', command: 'npm test -- --runInBand' } },
{ sequence: 4, kind: 'TOOL_OUTPUT', payload: { stdout: '276 passed, 4 skipped' } }
];
</script>

<svelte:head>
	<title>Computer visual regression fixtures</title>
</svelte:head>

<main
	class="app-theme min-h-screen bg-white p-4 font-sans text-gray-900 dark:bg-black dark:text-gray-100 sm:p-6"
>
	<div class="mx-auto grid max-w-6xl gap-6">
		<section
			data-testid="tools-surface"
			class="min-w-0 rounded-2xl border border-gray-200/70 p-4 dark:border-white/7"
		>
			<Tools />
		</section>

		<section
			data-testid="tool-servers-surface"
			class="min-w-0 rounded-2xl border border-gray-200/70 p-4 dark:border-white/7"
		>
			<ToolServers />
		</section>

		<section
			data-testid="terminal-surface"
			class="h-72 min-w-0 overflow-hidden rounded-2xl border border-gray-200/70 dark:border-white/7"
		>
			<Terminal
				sessionId="visual-regression"
				initialOutput="&#x1b[32m$&#x1b[0m npm run build&#13;&#10;Build finished successfully&#13;&#10;"
			/>
		</section>

		<section
			data-testid="tool-call-surface"
			class="min-w-0 rounded-2xl border border-gray-200/70 p-3 dark:border-white/7"
		>
			<ToolCallCollapsible
				item={toolCall}
				pairedOutput={toolOutput}
				done={true}
				chatId={null}
				messageId="visual-message"
				toolLabel={(name) => name}
				onapprove={() => {}}
			/>
		</section>

		<section
			data-testid="designer-results-surface"
			class="min-w-0 rounded-2xl border border-gray-200/70 p-3 dark:border-white/7"
		>
			<DesignerResults
				events={designerVisualFixture}
				status="verifying"
				runId="designer-visual-fixture"
				nativeMessageId="visual-assistant-message"
				onaction={() => {}}
				onreconnect={() => {}}
				oncancel={() => {}}
			/>
		</section>

<section
data-testid="live-terminal-surface"
class="min-w-0 rounded-2xl border border-gray-200/70 p-3 dark:border-white/7"
>
<LiveTerminal events={liveTerminalEvents} status="verifying" runId="run-live-terminal-fixture" />
</section>
	</div>
</main>
