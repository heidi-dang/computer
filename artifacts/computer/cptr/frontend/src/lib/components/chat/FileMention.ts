/**
 * TipTap Mention extension configured for @file mentions.
 *
 * Wraps @tiptap/extension-mention with file-specific rendering
 * and suggestion config wired to the workspace file search API.
 */

import Mention from '@tiptap/extension-mention';

export interface FileMentionAttrs {
	id: string; // full file path
	label: string; // display name (basename)
	type: string; // 'file' | 'directory'
}

/**
 * Create the configured Mention extension.
 * `suggestionOptions` is passed in from the component that owns
 * the lifecycle of the suggestion popup.
 */
export function createFileMention(suggestionOptions: {
	items: (props: { query: string }) => Promise<FileMentionAttrs[]> | FileMentionAttrs[];
	render: () => {
		onStart: (props: any) => void;
		onUpdate: (props: any) => void;
		onKeyDown: (props: any) => boolean;
		onExit: () => void;
	};
}) {
	return Mention.configure({
		HTMLAttributes: {
			class: 'bg-blue-500/10 text-blue-400 rounded px-1 py-px text-xs font-mono'
		},
		renderText({ node }) {
			const label = node.attrs.label ?? node.attrs.id;
			return `[${label}](file://${node.attrs.id})`;
		},
		suggestion: {
			char: '@',
			allowSpaces: false,
			...suggestionOptions
		}
	});
}

/**
 * Extract all file mention paths from a TipTap editor JSON doc.
 */
export function extractMentionedFiles(doc: any): string[] {
	const files: string[] = [];

	function walk(node: any) {
		if (node.type === 'mention' && node.attrs?.id) {
			files.push(node.attrs.id);
		}
		if (node.content) {
			for (const child of node.content) {
				walk(child);
			}
		}
	}

	if (doc) walk(doc);
	return [...new Set(files)];
}
