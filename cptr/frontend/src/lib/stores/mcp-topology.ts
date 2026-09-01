import type { McpTopologyConfig } from '$lib/apis/mcp';

export type McpTopologySelection = { kind: 'client' | 'node' | 'edge'; id: string } | null;

export type McpTopologyConfigState = {
	version: 1;
	canonicalLabels: Record<string, string>;
	aliases: Record<string, string>;
};

export function hydrateMcpTopologyConfig(config: McpTopologyConfig): McpTopologyConfigState {
	return {
		version: config.version,
		canonicalLabels: { ...config.canonical_labels },
		aliases: { ...config.aliases }
	};
}

export function displayTopologyLabel(
	id: string,
	canonical: string,
	aliases: Record<string, string>
): string {
	const alias = aliases[id]?.trim();
	return alias || canonical;
}
