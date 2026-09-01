export type McpToolArgumentSchema = {
	type?: string;
	enum?: unknown[];
};

export type McpToolArgumentCoercion = {
	args: Record<string, unknown> | null;
	error: string | null;
};

function objectRecord(value: unknown): value is Record<string, unknown> {
	return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function jsonError(error: unknown): string {
	return error instanceof Error ? `Invalid JSON: ${error.message}` : 'Invalid JSON';
}

export function coerceMcpToolArguments(input: {
	rawJsonMode: boolean;
	rawJson: string;
	formValues: Record<string, string>;
	properties: Record<string, McpToolArgumentSchema>;
	required: string[];
}): McpToolArgumentCoercion {
	if (input.rawJsonMode) {
		try {
			const parsed = JSON.parse(input.rawJson);
			if (!objectRecord(parsed)) {
				return { args: null, error: 'Arguments JSON must be an object.' };
			}
			return { args: parsed, error: null };
		} catch (error) {
			return { args: null, error: jsonError(error) };
		}
	}

	const args: Record<string, unknown> = {};
	for (const [key, schema] of Object.entries(input.properties)) {
		const raw = input.formValues[key] ?? '';
		if (raw === '' && !input.required.includes(key)) continue;
		const type = schema.type ?? 'string';

		if (type === 'number' || type === 'integer') {
			const value = Number(raw);
			if (!Number.isFinite(value) || (type === 'integer' && !Number.isInteger(value))) {
				return { args: null, error: `Invalid ${type} for ${key}.` };
			}
			args[key] = value;
			continue;
		}

		if (type === 'boolean') {
			if (raw !== 'true' && raw !== 'false') {
				return { args: null, error: `Invalid boolean for ${key}.` };
			}
			args[key] = raw === 'true';
			continue;
		}

		if (type === 'object' || type === 'array') {
			let parsed: unknown;
			try {
				parsed = JSON.parse(raw);
			} catch (error) {
				return {
					args: null,
					error: `Invalid JSON for ${key}: ${error instanceof Error ? error.message : 'parse failed'}`
				};
			}
			if (type === 'object' && !objectRecord(parsed)) {
				return { args: null, error: `${key} must be a JSON object.` };
			}
			if (type === 'array' && !Array.isArray(parsed)) {
				return { args: null, error: `${key} must be a JSON array.` };
			}
			args[key] = parsed;
			continue;
		}

		args[key] = raw;
	}

	return { args, error: null };
}

export function formatMcpToolArguments(args: Record<string, unknown> | null): string {
	return JSON.stringify(args ?? {}, null, 2);
}

export type McpSseFrame = {
	event: string;
	data: unknown;
};

function parseMcpSseBlock(block: string): McpSseFrame | null {
	let event = '';
	const dataLines: string[] = [];
	for (const rawLine of block.split('\n')) {
		const line = rawLine.trimEnd();
		if (line.startsWith('event:')) {
			event = line.slice('event:'.length).trim();
		} else if (line.startsWith('data:')) {
			dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
		}
	}
	if (!dataLines.length) return null;
	try {
		return { event, data: JSON.parse(dataLines.join('\n')) };
	} catch {
		return null;
	}
}

export function consumeMcpSseBuffer(
	buffer: string,
	flush = false
): { frames: McpSseFrame[]; remainder: string } {
	const normalized = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
	const parts = normalized.split('\n\n');
	let remainder = parts.pop() ?? '';
	if (flush && remainder.trim()) {
		parts.push(remainder);
		remainder = '';
	}

	const frames: McpSseFrame[] = [];
	for (const block of parts) {
		if (!block.trim()) continue;
		const frame = parseMcpSseBlock(block);
		if (frame) frames.push(frame);
	}
	return { frames, remainder };
}
