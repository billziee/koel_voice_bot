import { test, expect, type Page } from '@playwright/test';

// Navigation + shell E2E (roadmap Q-7). Deterministic coverage of the app shell:
// sidebar routing to each destination, the dark-mode toggle, and reload behaviour
// in the offline demo path. Like dashboard.spec these run backend-free — a failed
// login fetch falls back to demo mode, so no docker stack is required in CI.
//
// Intentionally excludes timing-gated dynamic sims (barge-in / escalation): those
// flake headless and are not deterministic. Everything here waits on a concrete,
// documented signal (a heading, an aria-label flip, the login screen) rather than
// on elapsed time.

// Sign in via the demo credentials. The "I'm human" gate (Item 21) is a simulated
// Turnstile: clicking the checkbox fires onChange, which swaps the input for an
// ~800ms "Verifying…" spinner and only then flips to "Verified". submit() rejects
// while humanVerified is still false, so we click the checkbox (not .check() — the
// input detaches, so .check() can never confirm its checked state) and wait for
// "Verified" before pressing Sign In. (Copied from dashboard.spec by design — each
// spec file stays self-contained.)
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

// Sign in and settle on the dashboard. The Bot Dashboard <h1> is the anchor the
// nav tests navigate away from, so wait for it before interacting with the shell.
async function signInToDashboard(page: Page): Promise<void> {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Bot Dashboard' })).toBeVisible({ timeout: 15_000 });
}

test('demo login lands on the Bot Dashboard as the default view', async ({ page }) => {
  await signInToDashboard(page);
  // The full app shell is present (sidebar sections render) and the dashboard is
  // the landing view — no navigation was needed to reach the heading above.
  await expect(page.getByText('Operate', { exact: true })).toBeVisible();
  await expect(page.getByText('Manage', { exact: true })).toBeVisible();
});

test('the sidebar opens Call History and renders its heading', async ({ page }) => {
  await signInToDashboard(page);

  await page.getByRole('button', { name: /call history/i }).first().click();
  // HistoryView is a React.lazy chunk — give the code-split bundle room to resolve.
  await expect(page.getByRole('heading', { name: /call history/i })).toBeVisible({ timeout: 15_000 });
});

test('the sidebar opens Knowledge Base and renders its heading', async ({ page }) => {
  await signInToDashboard(page);

  await page.getByRole('button', { name: /knowledge base/i }).first().click();
  // KnowledgeBaseView is also lazily loaded.
  await expect(page.getByRole('heading', { name: /knowledge base/i })).toBeVisible({ timeout: 15_000 });
});

test('the sidebar returns to the Dashboard after visiting another view', async ({ page }) => {
  await signInToDashboard(page);

  // Leave the dashboard for a lazy view…
  await page.getByRole('button', { name: /call history/i }).first().click();
  await expect(page.getByRole('heading', { name: /call history/i })).toBeVisible({ timeout: 15_000 });

  // …then come back. The eager Dashboard heading must re-render.
  await page.getByRole('button', { name: /bot dashboard/i }).first().click();
  await expect(page.getByRole('heading', { name: 'Bot Dashboard' })).toBeVisible({ timeout: 15_000 });
});

test('the dark-mode toggle flips the theme and back', async ({ page }) => {
  await signInToDashboard(page);

  // Documented signal: the header toggle's aria-label is "Switch to dark mode"
  // in light mode and "Switch to light mode" in dark mode (App.tsx). Flipping the
  // label is the stable, role-based proof the theme changed — no colour sampling.
  const toDark = page.getByRole('button', { name: /switch to dark mode/i });
  await expect(toDark).toBeVisible();
  await toDark.click();

  const toLight = page.getByRole('button', { name: /switch to light mode/i });
  await expect(toLight).toBeVisible();
  // Second signal: the root grid container gains the `dark` class in dark mode.
  await expect(page.locator('div.dark').first()).toBeVisible();

  // Toggling back restores the original control — confirms it is a real toggle.
  await toLight.click();
  await expect(page.getByRole('button', { name: /switch to dark mode/i })).toBeVisible();
});

test('a reload with no persisted session returns cleanly to the login screen', async ({ page }) => {
  await signInToDashboard(page);

  // Demo mode is stateless: handleLogin only persists a token when live+token are
  // set, so the offline path writes nothing to localStorage.
  await expect
    .poll(() => page.evaluate(() => window.localStorage.getItem('koel.access_token')))
    .toBeNull();

  await page.reload();

  // With no stored token there is no silent-refresh attempt, so the app lands on
  // the login form — not a stuck "Restoring session…" state and not the dashboard.
  await expect(page.getByRole('heading', { name: /sign in/i })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'KOEL' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Bot Dashboard' })).toHaveCount(0);
});
