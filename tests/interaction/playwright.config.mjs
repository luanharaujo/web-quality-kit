// The kit's own proof: the demo app (good and bad pages) under the checks.
import { defineConfig } from 'web-quality-kit-interaction';

const port = Number(process.env.PORT || 4791);
export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.mjs/,
  workers: 2,
  timeout: 30_000,
  reporter: 'line',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    // CHROME_PATH: an installed Chromium (the runners have one for Lighthouse)
    launchOptions: process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {},
  },
  webServer: {
    command: `node ${new URL('./demo/server.mjs', import.meta.url).pathname}`,
    url: `http://127.0.0.1:${port}/booking.html`,
    env: { PORT: String(port) },
    reuseExistingServer: false,
  },
});
