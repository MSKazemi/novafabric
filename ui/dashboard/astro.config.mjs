import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  site: 'https://novafabric.ai',
  integrations: [
    react(),
    // /dashboard/ is a noindex client-only app shell (see DashboardLayout.astro).
    sitemap({ filter: (page) => !page.endsWith('/dashboard/') }),
  ],
  vite: {
    plugins: [tailwindcss()],
  },
  build: {
    inlineStylesheets: 'auto',
  },
});
