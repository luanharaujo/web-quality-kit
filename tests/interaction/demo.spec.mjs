// Each page does the normal thing a person does; the kit's fixture checks it.
// The bad pages must be caught (check_report.mjs knows which rule each one
// breaks); the good pages must pass everything.
import { test, expect, expectIdempotent } from 'web-quality-kit-interaction';

const bookings = ['good', 'bad-busy', 'bad-double', 'bad-optimistic', 'bad-error'];

for (const v of bookings) {
  test(`booking ${v}`, async ({ page }) => {
    await page.goto(`/booking.html?v=${v}`);
    await page.getByLabel('Name').fill('Test User');
    await page.getByRole('button', { name: 'Book' }).click();
    // a person waits for the answer; on a failure pass the kit clicks retry
    await expect(page.getByRole('status')).toHaveText(/booked/i, { timeout: 8000 }).catch(() => {});
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

const count = (request, page) => async () => (await (await request.get(`/api/test/count?page=${page}`)).json()).n;

test('contract good', async ({ request }) => {
  await expectIdempotent(request, { url: '/api/good/bookings', data: { name: 'Ana' }, count: count(request, 'good') });
});

test('contract bad-double (server ignores the key)', async ({ request }) => {
  await expectIdempotent(request, { url: '/api/bad-double/bookings', data: { name: 'Ana' }, count: count(request, 'bad-double') });
});
