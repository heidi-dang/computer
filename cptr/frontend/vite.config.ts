import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit(), tailwindcss()],
	build: {
		// Intentional lazy editor/parser chunks can exceed Vite's generic 500 kB warning.
		// `build:clean` separately enforces a stricter 900 kB entry cap and 1.4 MB client-chunk cap.
		chunkSizeWarningLimit: 1400,
		rolldownOptions: {
			checks: {
				pluginTimings: false
			}
		}
	},
	server: {
		proxy: {
			'/api': {
				target: 'http://localhost:9741',
				changeOrigin: true,
				ws: true
			},
			'/v1': {
				target: 'http://localhost:9741',
				changeOrigin: true
			},
			'/socket.io': {
				target: 'http://localhost:9741',
				changeOrigin: true,
				ws: true
			}
		}
	}
});
