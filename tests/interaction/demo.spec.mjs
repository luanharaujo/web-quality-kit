// Each page does the normal thing a person does; the kit's fixture checks it.
// The bad pages must be caught (check_report.mjs knows which rule each one
// breaks); the good pages must pass everything.
import { test, expect, expectIdempotent, ciTimeout } from 'web-quality-kit-interaction';

const bookings = ['good', 'good-reads', 'good-key', 'good-bg', 'bad-busy', 'bad-double', 'bad-optimistic', 'bad-error', 'bad-request-id'];

for (const v of bookings) {
  test(`booking ${v}`, async ({ page }) => {
    await page.goto(`/booking.html?v=${v}`);
    await page.getByLabel('Name').fill('Test User');
    await page.getByRole('button', { name: 'Book' }).click();
    // a person waits for the answer; on a failure pass the kit clicks retry
    await expect(page.getByRole('status')).toHaveText(/booked/i, { timeout: 8000 }).catch(() => {}); // kit:short-timeout fails on purpose on the bad pages
  });
}

const png = (name) => ({ name, mimeType: 'image/png', buffer: Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da6364f8ffff3f0005fe02fea7d6a4c80000000049454e44ae426082', 'hex') });

for (const v of ['good', 'bad']) {
  test(`upload ${v}`, async ({ page }) => {
    await page.goto(`/upload.html?v=${v}`);
    await page.getByLabel('Files').setInputFiles([png('a.png'), png('b.png'), png('c.png')]);
    await page.waitForTimeout(4000);
  });
}

for (const v of ['list-good', 'list-dup', 'list-noload', 'list-search', 'list-readonly', 'list-prefilled', 'list-excluded', 'list-poll', 'list-static', 'list-hidden']) {
  test(`list ${v}`, async ({ page }) => {
    await page.goto(`/list.html?v=${v}`);
    await expect(page.getByText('Partner balance')).toBeVisible({ timeout: ciTimeout(8000) });
  });
}

// The test answers the read itself, after a wait: the loading signal shown
// when the read left must still count.
test('list list-mocked (slow route handler in the test)', async ({ page }) => {
  await page.route('**/api/list-mocked/items*', async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({ json: ['Handstand basics', 'Partner balance'] });
  });
  await page.goto('/list.html?v=list-mocked');
  await expect(page.getByText('Partner balance')).toBeVisible({ timeout: ciTimeout(8000) });
});

// Typing starts a read; the draft saved later, with no tap, is not a tap's write.
test('booking good-typed (autosave after typing)', async ({ page }) => {
  await page.goto('/booking.html?v=good-typed');
  await page.getByLabel('Name').pressSequentially('Ana B', { delay: 50 }); // a space is typing, not a tap
  await page.waitForTimeout(3000);
});

// A test with a fixed clock: the loading check must still see the signal.
test('list list-clock (page.clock)', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-01-05T10:00:00Z'));
  await page.goto('/list.html?v=list-clock');
  await expect(page.getByText('Partner balance')).toBeVisible({ timeout: 8000 });
});

// The test ends (and the page closes) while a slow read is out: no verdict.
test('list list-closed (page closed during the check)', async ({ page }) => {
  await page.route('**/api/list-closed/items*', async (route) => {
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({ json: [] }).catch(() => {});
  });
  await page.goto('/list.html?v=list-closed', { waitUntil: 'commit' });
  await page.close();
});

// A mocked read that never answers (a hung server) must not hang the test.
test('list list-hung (read never answers)', async ({ page }) => {
  await page.route('**/api/list-hung/items*', () => new Promise(() => {}));
  await page.goto('/list.html?v=list-hung');
  await page.waitForTimeout(1500);
});

test('list list-filter (A, B, A again is three actions)', async ({ page }) => {
  await page.goto('/list.html?v=list-filter');
  await expect(page.getByText('Partner balance')).toBeVisible({ timeout: ciTimeout(8000) });
  for (const level of ['b', 'a', 'b']) {
    await page.getByLabel('Level').selectOption(level);
    await expect(page.getByRole('status')).toHaveText('', { timeout: ciTimeout(8000) });
  }
});

const count = (request, page) => async () => (await (await request.get(`/api/test/count?page=${page}`)).json()).n;

test('contract good', async ({ request }) => {
  await expectIdempotent(request, { url: '/api/good/bookings', data: { name: 'Ana' }, count: count(request, 'good') });
});

test('contract bad-double (server ignores the key)', async ({ request }) => {
  await expectIdempotent(request, { url: '/api/bad-double/bookings', data: { name: 'Ana' }, count: count(request, 'bad-double') });
});
