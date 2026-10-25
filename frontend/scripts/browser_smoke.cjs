const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

let sensitiveValue = '';
async function main() {
  const base = process.env.CLOUDSRE_BASE_URL || 'http://localhost:18080';
  const accessFile = process.env.CLOUDSRE_ACCESS_FILE || 'D:/uclaude/console-access.txt';
  const token = fs.readFileSync(accessFile, 'utf8').split(/\r?\n/).find(x => x.startsWith('访问码：'))?.slice(4);
  if (!token) throw new Error('Local console access code missing');
  sensitiveValue = token;
  const temp = process.env.CLOUDSRE_BROWSER_TEMP || 'D:/uclaude/browser-temp';
  fs.mkdirSync(temp, { recursive: true }); process.env.TEMP = temp; process.env.TMP = temp;
  const browser = await chromium.launch({ channel: process.env.CLOUDSRE_BROWSER_CHANNEL || 'chrome', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 980 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const checks = [];
  async function check(name, fn) { await fn(); checks.push({ name, passed: true }); }
  try {
    await page.goto(base);
    await check('actual_login', async () => {
      await page.getByLabel('控制台访问码', { exact: true }).fill(token);
      await page.getByRole('button', { name: '进入指挥台', exact: true }).click();
      await page.getByRole('heading', { name: '看清故障，再决定变更。' }).waitFor();
    });
    await check('history_and_evidence', async () => {
      await page.getByRole('button', { name: '全部与历史', exact: true }).click();
      await page.getByLabel('状态', { exact: true }).selectOption('RESOLVED');
      await page.locator('.event-row').first().click();
      await page.getByRole('tab', { name: '证据', exact: true }).click();
      await page.locator('.evidence-index button').first().waitFor();
      await page.locator('.evidence-record .json-view').waitFor();
    });
    await check('report_download', async () => {
      await page.getByRole('tab', { name: '报告与用量' }).click();
      const download = page.waitForEvent('download');
      await page.getByRole('button', { name: '导出 JSON' }).click();
      const file = await download;
      const target = path.join(temp, file.suggestedFilename());
      await file.saveAs(target);
      const report = JSON.parse(fs.readFileSync(target, 'utf8'));
      if (report.verification_claim !== 'verified_recovery') throw new Error('Unexpected historical report outcome');
    });
    await check('creation_form_default_is_manual', async () => {
      await page.getByRole('button', { name: '发起调查', exact: true }).click();
      await page.getByRole('heading', { name: '发起事件调查' }).waitFor();
      if (!await page.getByRole('radio', { name: /逐次人工审批/ }).isChecked()) throw new Error('Manual approval is not the default');
    });
    if (errors.length) throw new Error('Browser exceptions: ' + errors.join('; '));
    console.log(JSON.stringify({ passed: true, checks, browser_exceptions: errors }));
  } finally { await context.close(); await browser.close(); }
}
main().catch(e => { console.error(sensitiveValue ? e.message.replaceAll(sensitiveValue, "[REDACTED]") : e.message); process.exitCode = 1; });
