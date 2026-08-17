import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/postcss';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	css: {
		postcss: {
			plugins: [tailwindcss()]
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
