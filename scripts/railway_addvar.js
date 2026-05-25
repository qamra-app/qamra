const { chromium } = require('playwright');

(async () => {
  const context = await chromium.launchPersistentContext(
    'C:\\Users\\aalad\\AppData\\Local\\Google\\Chrome\\User Data',
    {
      headless: false,
      channel: 'chrome',
      args: ['--profile-directory=Default'],
    }
  );

  const page = await context.newPage();
  console.log('Opening Railway...');
  await page.goto('https://railway.app/dashboard', { waitUntil: 'networkidle', timeout: 20000 });
  await page.waitForTimeout(3000);
  console.log('URL:', page.url());
  await page.screenshot({ path: 'railway_step1.png' });

  // Try to find the qamra project
  try {
    await page.click('text=qamra', { timeout: 8000 });
    await page.waitForTimeout(3000);
    console.log('Clicked qamra, URL:', page.url());
    await page.screenshot({ path: 'railway_step2.png' });
  } catch(e) {
    console.log('Could not find qamra project:', e.message);
    await page.screenshot({ path: 'railway_step2_fail.png' });
    await context.close();
    return;
  }

  // Click on the service
  try {
    await page.click('text=wedding', { timeout: 5000 });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'railway_step3.png' });
    console.log('Clicked service, URL:', page.url());
  } catch(e) {
    console.log('Could not find wedding service, trying screenshot:', e.message);
    await page.screenshot({ path: 'railway_step3_fail.png' });
  }

  // Look for Variables tab
  try {
    await page.click('text=Variables', { timeout: 5000 });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'railway_step4.png' });
    console.log('On Variables tab');
  } catch(e) {
    console.log('Could not find Variables tab:', e.message);
    await page.screenshot({ path: 'railway_step4_fail.png' });
  }

  console.log('Done - check screenshots');
  await context.close();
})();
