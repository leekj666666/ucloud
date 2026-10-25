import { test, expect, type Page } from '@playwright/test';

async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('控制台访问码', { exact: true }).fill('browser-contract-access');
  await page.getByRole('button', { name: '进入指挥台', exact: true }).click();
  await expect(page.getByRole('button', { name: '发起调查', exact: true })).toBeVisible();
}
async function openCase(page: Page, label: string) {
  label += ' · ' + test.info().project.name;
  await login(page);
  await page.getByRole('button', { name: new RegExp('浏览器测试夹具 · ' + label) }).click();
  await expect(page.getByRole('heading', { name: '浏览器测试夹具 · ' + label })).toBeVisible();
}

test('keyboard login, filtering, empty state and manual creation defaults', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('控制台访问码', { exact: true }).fill('错误访问码');
  await page.getByLabel('控制台访问码', { exact: true }).press('Enter');
  await expect(page.getByRole('alert')).toContainText('访问码不正确');
  await page.getByLabel('控制台访问码', { exact: true }).fill('browser-contract-access');
  await page.getByLabel('控制台访问码', { exact: true }).press('Enter');
  await page.getByRole('button', { name: '发起调查', exact: true }).click();
  await expect(page.getByRole('radio', { name: /逐次人工审批/ })).toBeChecked();
  await page.getByRole('radio', { name: /演练自动执行/ }).check();
  await page.getByLabel('观察到的问题', { exact: true }).fill('浏览器排队测试 · ' + test.info().project.name);
  await expect(page.getByRole('button', { name: '开始调查', exact: true })).toBeDisabled();
  await page.getByRole('radio', { name: /逐次人工审批/ }).check();
  await page.getByRole('button', { name: '开始调查', exact: true }).click();
  await expect(page.getByText('等待工作器', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '停止任务', exact: true }).click();
  await page.getByLabel('停止原因', { exact: true }).fill('测试离线工作器排队任务停止');
  await page.getByRole('button', { name: '确认停止后续任务', exact: true }).click();
  await expect(page.getByText('停止请求已记录。正在等待在途操作确认，不会再开始新变更。', { exact: true })).toBeVisible();
  const search = page.getByRole('textbox', { name: /搜索/ });
  await search.fill('不存在的事件');
  await expect(page.getByText('没有匹配事件', { exact: true })).toBeVisible();
});

test('approval is explicit, immutable and contains no implicit verification claim', async ({ page }) => {
  await openCase(page, '批准测试');
  const approve = page.getByRole('button', { name: '批准并执行', exact: true });
  await expect(approve).toBeDisabled();
  await page.getByRole('checkbox', { name: /我已核对目标/ }).check();
  await approve.click();
  await expect(page.getByText('操作员已批准本方案', { exact: true })).toBeVisible();
  await expect(approve).toHaveCount(0);
  await page.getByRole('tab', { name: '业务验收', exact: true }).click();
  await expect(page.getByText('尚未完成业务验收', { exact: true })).toBeVisible();
});

test('reject requires a reason and records the bound decision', async ({ page }) => {
  await openCase(page, '拒绝测试');
  await page.getByRole('button', { name: '拒绝此方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '确认拒绝', exact: true })).toBeDisabled();
  await page.getByLabel('拒绝原因', { exact: true }).fill('浏览器契约测试拒绝');
  await page.getByRole('button', { name: '确认拒绝', exact: true }).click();
  await expect(page.getByText('操作员已拒绝本方案', { exact: true })).toBeVisible();
});

test('offline display preserves data and blocks approval', async ({ page }) => {
  await openCase(page, '断连测试');
  await page.getByRole('checkbox', { name: /我已核对目标/ }).check();
  await page.route('**/console/incidents/**', route => route.abort('failed'));
  await expect(page.getByText('数据连接已中断，请刷新后再审批。', { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: '批准并执行', exact: true })).toBeDisabled();
  await expect(page.getByRole('heading', { name: '浏览器测试夹具 · 断连测试' })).toBeVisible();
});

test('stale plan conflict surfaces a recoverable error', async ({ page }) => {
  await openCase(page, '冲突测试');
  await page.route('**/console/incidents/*/decisions', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'Plan identity or version changed; reload the plan' }) }));
  await page.getByRole('checkbox', { name: /我已核对目标/ }).check();
  await page.getByRole('button', { name: '批准并执行', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('方案版本已变化');
});

test('untrusted text stays inert and both exports preserve incomplete scope', async ({ page }) => {
  await openCase(page, '文本与导出测试');
  await page.getByText('查看模型完整判断与排除依据', { exact: true }).click();
  await expect(page.getByText(/仅用于浏览器契约测试的判断/)).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as Record<string, unknown>).fixtureXss)).toBeUndefined();
  await page.getByRole('tab', { name: '报告与用量', exact: true }).click();
  await expect(page.getByText('尚未确认恢复', { exact: true })).toBeVisible();
  for (const name of ['导出 JSON', '导出 Markdown']) {
    const received = page.waitForEvent('download');
    await page.getByRole('button', { name, exact: true }).click();
    const download = await received;
    expect(await download.failure()).toBeNull();
    expect(download.suggestedFilename()).toMatch(/incident-.*\.(json|md)$/);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
