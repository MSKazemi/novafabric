import { defineConfig, devices } from '@playwright/test';

// PW_PORT lets a run use a free port when 4321 is occupied by something that
// is NOT the local preview build (e.g. on n1 the deployed `nova serve`
// container listens on 4321 and `reuseExistingServer` would silently test the
// deployed bundle instead of the working tree). Default behavior unchanged.
const PORT = Number(process.env.PW_PORT ?? 4321);

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: process.env.LH_NO_SERVER
    ? undefined
    : {
        command: `npm run preview -- --port ${PORT}`,
        url: `http://127.0.0.1:${PORT}`,
        reuseExistingServer: !process.env.CI,
        timeout: 30_000,
      },
});
