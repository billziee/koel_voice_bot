import { test, expect, type Page } from '@playwright/test';

// Critical-flow E2E (R). The offline demo path needs no backend: a failed login
// fetch falls back to demo mode, so these run in CI without the docker stack.

// Sign in via the demo credentials. The "I'm human" gate (Item 21) is a
// simulated Turnstile: clicking the checkbox fires onChange, which swaps the
// input out for an ~800ms "Verifying…" spinner and only then flips to
// "Verified". submit() rejects while humanVerified is still false, so we must
// click the checkbox (not .check() — the input detaches, so .check() can never
// confirm its checked state) and wait for "Verified" before pressing Sign In.
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

test('login screen renders with seeded-credential hint', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'KOEL' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible();
  await expect(page.getByText(/demo \/ koel-demo/)).toBeVisible();
});

test('demo login reaches the bot dashboard', async ({ page }) => {
  await signIn(page);

  // Demo mode (backend offline) still lands on the dashboard with bot cards.
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
});

test('knowledge base view is reachable from the nav', async ({ page }) => {
  await signIn(page);

  await page.getByRole('button', { name: /knowledge base/i }).first().click();
  await expect(page.getByRole('heading', { name: /knowledge base/i })).toBeVisible();
});

test('no console errors on initial load', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'KOEL' })).toBeVisible();
  expect(errors, errors.join('\n')).toHaveLength(0);
});
