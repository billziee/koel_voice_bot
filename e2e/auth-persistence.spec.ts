import { test, expect, type Page } from '@playwright/test';

// JWT persistence + silent refresh (roadmap Q-2). The backend is mocked so the
// FE runs in LIVE mode without a stack: login yields a token, the token is
// persisted, and a reload restores the session via /v1/auth/refresh instead of
// bouncing the operator back to the login screen.

// A well-formed but unsigned JWT whose payload carries a future `exp`. The FE
// only base64-decodes the payload (to schedule refresh) — it never verifies the
// signature — so this is enough to exercise the persistence path deterministically.
function fakeJwt(expEpochSec: number): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'demo', role: 'operator', exp: expEpochSec })}.sig`;
}

async function mockBackend(page: Page): Promise<void> {
  const future = Math.floor(Date.now() / 1000) + 900; // 15-min access TTL
  // Regexes (matched against the full URL) are more reliable than globs here,
  // since the FE calls an absolute origin (http://localhost:8100).
  await page.route(/\/v1\/auth\/login$/, (r) => r.fulfill({ json: { access_token: fakeJwt(future) } }));
  await page.route(/\/v1\/auth\/refresh$/, (r) => r.fulfill({ json: { access_token: fakeJwt(future) } }));
  await page.route(/\/v1\/bots$/, (r) => r.fulfill({ json: [] }));
  await page.route(/\/v1\/stats$/, (r) => r.fulfill({ json: [] }));
}

async function signIn(page: Page): Promise<void> {
  await page.goto('/');
  await page.locator('#login-email').fill('demo');
  await page.locator('#login-password').fill('koel-demo');
  const human = page.getByRole('checkbox', { name: /human/i });
  if (await human.count()) {
    await human.click();
    await expect(page.getByText('Verified', { exact: true })).toBeVisible({ timeout: 5_000 });
  }
  await page.getByRole('button', { name: /sign in/i }).click();
}

const readToken = (page: Page) => page.evaluate(() => window.localStorage.getItem('koel.access_token'));

test('live session persists across a reload via silent refresh', async ({ page }) => {
  await mockBackend(page);
  await signIn(page);
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
  // The access token must be persisted (not just held in React state). Poll —
  // handleLogin writes it around the same tick the dashboard renders.
  await expect.poll(() => readToken(page)).toBeTruthy();

  await page.reload();

  // After reload the dashboard must come back (session restored via /auth/refresh)
  // — NOT the login form.
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: /sign in/i })).toHaveCount(0);
});

test('a stored-but-invalid token falls back to login (not a crash)', async ({ page }) => {
  // Seed an expired/garbage token; the restore refresh 401s → clear it → login.
  await page.route(/\/v1\/auth\/refresh$/, (r) => r.fulfill({ status: 401, json: { detail: 'expired' } }));
  await page.addInitScript(() => window.localStorage.setItem('koel.access_token', 'stale.token.value'));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible({ timeout: 15_000 });
  // The dead token must be cleared so it isn't retried forever.
  await expect.poll(() => readToken(page)).toBeNull();
});
