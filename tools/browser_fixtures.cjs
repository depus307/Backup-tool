// Test the fixture server's dynamic behavior, not future capture code.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.BACKUPTOOL_PLAYWRIGHT_JS);

(async () => {
  const [base, output] = process.argv.slice(2);
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const report = {};
  for (const channel of ['chrome', 'msedge']) {
    let browser;
    try {
      browser = await chromium.launch({channel, headless:true});
      const context = await browser.newContext();
      const external = [];
      await context.route('**/*', route => {
        if (new URL(route.request().url()).origin === base) return route.continue();
        external.push(route.request().url()); return route.abort();
      });
      const page = await context.newPage();
      await page.goto(base + '/spa');
      await page.getByText('SPA-READY:', {exact:false}).waitFor();
      await page.goto(base + '/js-redirect');
      await page.waitForURL(base + '/simple?via=js');
      assert.match(await page.textContent('body'), /SIMPLE-READY/);
      await page.goto(base + '/redirect');
      assert.equal(page.url(), base + '/simple?via=http');
      await page.goto(base + '/typography');
      await page.evaluate(() => document.fonts.ready);
      const resources = await page.evaluate(() => ({
        font:document.fonts.check('18px Fixture'),
        image:document.querySelector('img').naturalWidth,
        fontLoaded:[...document.fonts].some(f=>f.family==='Fixture' && f.status==='loaded')
      }));
      assert.equal(resources.fontLoaded, true); assert.equal(resources.image, 240);
      await page.goto(base + '/lazy');
      await page.locator('#lazy').scrollIntoViewIfNeeded();
      await page.waitForFunction(() => document.querySelector('#lazy').naturalWidth === 240);
      await page.goto(base + '/challenge');
      assert.match(await page.textContent('body'), /CHALLENGE-PENDING/);
      await page.getByRole('button').click();
      assert.match(await page.textContent('body'), /MANUAL-READY/);
      assert.doesNotMatch(await page.textContent('body'), /CHALLENGE-PENDING/);
      assert.deepEqual(external, []);
      report[channel] = {status:'PASS', version:browser.version(),
        scenarios:['SPA','HTTP redirect','JS redirect','local webfont','image','lazy image','test challenge state'],
        resources, external_requests:external};
    } catch(error) {
      report[channel] = {status:'FAIL', error:error.stack};
    } finally {
      if(browser) await browser.close();
    }
  }
  fs.mkdirSync(path.dirname(output), {recursive:true});
  fs.writeFileSync(output, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  process.exitCode = Object.values(report).some(r=>r.status!=='PASS') ? 1 : 0;
})();
