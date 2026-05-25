const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto('https://railway.app/dashboard', { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);
  console.log('URL:', page.url());
  await page.screenshot({ path: 'railway_dashboard.png' });
  console.log('Screenshot saved');

  await browser.close();
})();
