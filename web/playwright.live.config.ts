import { defineConfig, devices } from '@playwright/test';

// Live smoke suite: drives a REAL `nova serve` (booted by tests/live/serve-fixture.ts on a
// free port, over a throwaway NOVAFABRIC_HOME). Separate from playwright.config.ts, whose
// tests/e2e specs use canned API responses against the static preview build.
//
//   npm run test:live            # skips (does not fail) when no browser / no novafabric
//   PW_CHANNEL=chrome npm run test:live   # use an installed Chrome
//
// Not part of any default CI job.
export default defineConfig({
  testDir: './tests/live',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: { trace: 'retain-on-failure' },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        ...(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {}),
      },
    },
  ],
});
