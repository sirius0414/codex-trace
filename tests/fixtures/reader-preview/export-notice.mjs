/** GUI-03: real production layout and hit-testing, with synthetic export feedback only. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

export async function runExportNoticeAcceptance(page, artifactDir) {
  const checks = [];
  const check = (name, value) => { assert.ok(value, name); checks.push(name); };
  const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const result = `Export failed: all original sources are unreadable. No local save was requested.\nC:\\Users\\Synthetic\\Downloads\\Codex Trace\\${'long-path-'.repeat(80)}.zip\nSHA-256 ${'a'.repeat(64)}`;
  await mkdir(artifactDir, { recursive: true });
  await page.reload();
  await page.waitForFunction(() => Boolean(window.readerFixture));
  await page.evaluate(() => window.readerFixture.setLanguage('en'));
  await settle();
  for (const [width, height] of [[537, 700], [390, 700], [980, 700], [537, 300]]) {
    await page.cdp('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    const zoom = width / await page.evaluate(() => innerWidth);
    await page.cdp('Emulation.setDeviceMetricsOverride', { width: Math.round(width * zoom), height: Math.round(height * zoom), deviceScaleFactor: 1, mobile: false });
    await page.evaluate(message => window.readerFixture.setExportMessage(message), result);
    await settle();
    const geometry = await page.evaluate(() => {
      const notice = document.querySelector('.cx-export-message').getBoundingClientRect();
      const usage = document.querySelector('.cx-usage-bar').getBoundingClientRect();
      const button = document.querySelector('.cx-usage-trigger');
      const rect = button.getBoundingClientRect();
      return { width: innerWidth, height: innerHeight, usageBottom: usage.bottom, noticeBottom: notice.bottom, usageTop: usage.top,
        hit: button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)),
        noticeWidth: notice.width, panelWidth: document.querySelector('.cx-panel').clientWidth,
        textHeight: document.querySelector('.cx-export-message-text').clientHeight };
    });
    check(`${width}×${height}: notice has its own row above usage`, geometry.width === width && geometry.noticeBottom <= geometry.usageTop + 0.5);
    check(`${width}×${height}: usage button is visible and wins the hit test`, geometry.hit && geometry.usageBottom <= geometry.height + 0.5);
    check(`${width}×${height}: long receipts stay bounded`, geometry.noticeWidth <= geometry.panelWidth + 0.5 && geometry.textHeight <= 72);
    await page.click('.cx-usage-trigger');
    check(`${width}×${height}: usage notes open while the export result remains visible`, await page.evaluate(() => Boolean(document.querySelector('.cx-usage-popover')) && Boolean(document.querySelector('.cx-export-message'))));
    await page.click('.cx-usage-trigger');
  }
  await page.focus('[aria-label="Dismiss export notice"]');
  await page.press('[aria-label="Dismiss export notice"]', 'Enter');
  await settle();
  check('keyboard dismissal removes the notice and returns focus to Export', await page.evaluate(() => !document.querySelector('.cx-export-message') && document.activeElement?.matches('.cx-download')));
  await page.evaluate(() => window.readerFixture.refresh());
  await settle();
  check('normal snapshot refresh does not reopen a dismissed result', await page.evaluate(() => !document.querySelector('.cx-export-message')));
  await page.click('.cx-download');
  await settle();
  check('a new export displays progress after dismissal', await page.evaluate(() => document.querySelector('.cx-export-message-text')?.textContent === 'Collecting synthetic export…'));
  await page.evaluate(message => { window.readerFixture.setExporting(false); window.readerFixture.setExportMessage(message); }, result);
  await settle();
  check('the next result reappears even when it repeats the previous error', await page.evaluate(message => document.querySelector('.cx-export-message-text')?.textContent === message, result));
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: 537, height: 700, deviceScaleFactor: 1, mobile: false });
  const zoom = 537 / await page.evaluate(() => innerWidth);
  await page.cdp('Emulation.setDeviceMetricsOverride', { width: Math.round(537 * zoom), height: Math.round(700 * zoom), deviceScaleFactor: 1, mobile: false });
  await settle();
  const size = await page.evaluate(() => ({ width: Math.ceil(innerWidth * devicePixelRatio), height: Math.ceil(innerHeight * devicePixelRatio) }));
  const screenshot = await page.cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: { x: 0, y: 0, ...size, scale: 1 } });
  await writeFile(`${artifactDir}/export-notice-537px.png`, Buffer.from(screenshot.data, 'base64'));
  await writeFile(`${artifactDir}/export-notice-checks.json`, JSON.stringify(checks, null, 2));
  await page.cdp('Emulation.clearDeviceMetricsOverride');
  return checks;
}
