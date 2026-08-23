/**
 * TipTap Mention extension configured for $skill mentions.
 *
 * Similar to FileMention but uses '$' as the trigger character
 * and wired to the skills API for suggestions.
 */

import Mention from '@tiptap/extension-mention';

export interface SkillMentionAttrs {
	id: string; // skill name
	label: string; // display name
	description?: string; // skill description (for popup)
	source?: string; // "workspace" | "global" (for popup badge)
}

/**
 * Create the configured Mention extension for skills.
 * Uses '$' as the trigger character.
 */
export function createSkillMention(suggestionOptions: {
	items: (props: { query: string }) => Promise<SkillMentionAttrs[]> | SkillMentionAttrs[];
	render: () => {
		onStart: (props: any) => void;
		onUpdate: (props: any) => void;
		onKeyDown: (props: any) => boolean;
		onExit: () => void;
	};
}) {
	return Mention.extend({ name: 'skillMention' }).configure({
		HTMLAttributes: {
			class: 'bg-purple-500/10 text-purple-400 rounded px-1 py-px text-xs font-mono'
		},
		renderText({ node }) {
			return `$${node.attrs.label ?? node.attrs.id}`;
		},
		suggestion: {
			char: '$',
			allowSpaces: false,
			...suggestionOptions
		}
	});
}

/**
 * Extract all skill mention names from a TipTap editor JSON doc.
 */
export function extractMentionedSkills(doc: any): string[] {
	const skills: string[] = [];

	function walk(node: any) {
		if (node.type === 'skillMention' && node.attrs?.id) {
			skills.push(node.attrs.id);
		}
		if (node.content) {
			for (const child of node.content) {
				walk(child);
			}
		}
	}

	if (doc) walk(doc);
	return [...new Set(skills)];
}
