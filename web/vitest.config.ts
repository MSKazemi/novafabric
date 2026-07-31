/// <reference types="vitest" />
import { getViteConfig } from 'astro/config';

// Unit tests for the dashboard SPA. Uses Astro's own Vite pipeline so JSX,
// path aliases (@/*), and Tailwind imports resolve exactly as in the app.
export default getViteConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/unit/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/unit/setup.ts'],
    globals: true,
  },
});
