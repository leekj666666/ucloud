import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  expect: { timeout: 7_000 },
  retries: 0,
  workers: 1,
  reporter: [['list'], ['junit', { outputFile: '../ci-evidence/browser-junit.xml' }]],
  outputDir: '../ci-evidence/browser-results',
  use: { baseURL: 'http://localhost:18089', trace: 'retain-on-failure', screenshot: 'only-on-failure', channel: process.env.CLOUDSRE_CHROME_CHANNEL || undefined },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'] } }, { name: 'mobile', use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' } }],
  webServer: process.env.CLOUDSRE_UI_TEST_EXTERNAL ? undefined : {
    command: '../.venv/bin/python ../scripts/console_browser_fixture.py',
    env: { PYTHONPATH: '../backend' },
    url: 'http://localhost:18089/health', timeout: 30_000, reuseExistingServer: false,
  },
});
