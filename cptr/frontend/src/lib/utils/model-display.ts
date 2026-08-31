export interface ModelDisplayInfo {
	id: string;
	name?: string;
	provider?: string;
	connection_id?: string;
	source_name?: string;
	agent_id?: string;
	profile_id?: string;
}

const SOURCE_LABELS: Record<string, string> = {
	openai: 'OpenAI',
	anthropic: 'Anthropic',
	deepseek: 'DeepSeek',
	google: 'Google',
	gemini: 'Gemini',
	openrouter: 'OpenRouter',
	ollama: 'Ollama',
	'openai-compatible': 'OpenAI Compatible',
	codex: 'Codex',
	claude: 'Claude',
	claude_code: 'Claude Code',
	hermes: 'Hermes',
	opencode: 'OpenCode'
};

export function formatSourceName(value: string): string {
	const trimmed = value.trim();
	if (!trimmed) return 'Other';
	const alias = SOURCE_LABELS[trimmed.toLowerCase()];
	if (alias) return alias;
	return trimmed.replace(/[_-]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

export function sourceKey(model: ModelDisplayInfo): string {
	if (model.provider === 'agent') {
		return `agent:${(model.agent_id || model.profile_id || 'agent').toLowerCase()}`;
	}
	return `connection:${(
		model.connection_id ||
		model.source_name ||
		model.provider ||
		'other'
	).toLowerCase()}`;
}

export function sourceLabel(model: ModelDisplayInfo): string {
	if (model.provider === 'agent') {
		return formatSourceName(model.agent_id || model.profile_id || 'Agent');
	}
	const configuredName = model.source_name?.trim();
	return configuredName || formatSourceName(model.provider || 'Other');
}

export function displayModelName(model: ModelDisplayInfo): string {
	if (model.provider !== 'agent') return model.name || model.id;
	for (const candidate of [model.name, model.id]) {
		if (!candidate?.startsWith('agent:')) continue;
		const slash = candidate.indexOf('/');
		if (slash >= 0 && slash < candidate.length - 1) return candidate.slice(slash + 1);
	}
	return model.name || model.id;
}

export function modelSearchText(model: ModelDisplayInfo): string {
	return [
		displayModelName(model),
		model.name,
		model.id,
		model.provider,
		model.source_name,
		model.agent_id,
		model.profile_id,
		sourceLabel(model)
	]
		.filter(Boolean)
		.join(' ')
		.toLowerCase();
}

export function groupModelsBySource<T extends ModelDisplayInfo>(
	models: readonly T[]
): { key: string; label: string; models: T[] }[] {
	const groups = new Map<string, { key: string; label: string; models: T[] }>();
	for (const model of models) {
		const key = sourceKey(model);
		const existing = groups.get(key);
		if (existing) {
			existing.models.push(model);
		} else {
			groups.set(key, { key, label: sourceLabel(model), models: [model] });
		}
	}
	return Array.from(groups.values());
}
