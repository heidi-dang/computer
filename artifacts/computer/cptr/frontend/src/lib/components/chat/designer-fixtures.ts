export const designerVisualFixture = [
	{
		id: 'designer-system-1',
		kind: 'DESIGN_SYSTEM_EXTRACTED',
		sequence: 1,
		payload: {
			design_system: {
				name: 'Cedar / Signal',
				tokens: {
					'color.canvas': '#f1e7d6',
					'color.ink': '#283934',
					'color.signal': '#c95f49',
					'color.moss': '#6f8a73',
					'radius.card': '14px',
					'space.gutter': '24px'
				}
			}
		}
	},
	{
		id: 'designer-variants-1',
		kind: 'DESIGN_VARIANTS_READY',
		sequence: 2,
		payload: {
			variants: [
				{ id: 'quiet-ledger', name: 'Quiet ledger', description: 'A measured reading rhythm with warm surfaces.', confidence: 'high', source: 'reference set A' },
				{ id: 'signal-editorial', name: 'Signal editorial', description: 'Sharper contrast for a more expressive first fold.', confidence: 'medium', source: 'reference set B' }
			]
		}
	},
	{
		id: 'designer-reconstruction-1',
		kind: 'SCREENSHOT_TO_UI_READY',
		sequence: 3,
		payload: {
			reconstruction: {
				title: 'Workspace home',
				status: 'review',
				summary: 'Hierarchy reconstructed from the supplied desktop screenshot.',
				source: 'home-reference.png',
				component_count: 11
			},
			viewports: [
				{ viewport: 'Desktop · 1440', status: 'passed', score: 98 },
				{ viewport: 'Tablet · 834', status: 'passed', score: 91 },
				{ viewport: 'Mobile · 390', status: 'unverified', score: 64 }
			],
			comparison: {
				title: 'One spacing drift found',
				status: 'needs repair',
				score: '91.4%',
				recommendation: 'The mobile gutter needs a second pass.',
				evidence: ['pixel diff: 8.6%', 'source: home-reference.png']
			},
			evidence: ['screenshot://home-reference.png', 'viewport://390x844']
		}
	}
] as const;