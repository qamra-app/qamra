const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();

  // Try to use existing session/cookies
  const page = await context.newPage();
  await page.goto('https://app.wassenger.com/devices', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);

  const url = page.url();
  console.log('Current URL:', url);

  // Take screenshot
  await page.screenshot({ path: 'wassenger_check.png', fullPage: false });
  console.log('Screenshot saved');

  await browser.close();
})();
