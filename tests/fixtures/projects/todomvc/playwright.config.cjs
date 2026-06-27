const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  timeout: 5000,
  expect: {
    timeout: 2000,
  },
  webServer: {
    command: 'python3 -m http.server 4173',
    port: 4173,
    reuseExistingServer: false,
  },
  use: {
    baseURL: 'http://127.0.0.1:4173',
  },
});
