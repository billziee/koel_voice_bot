import { test, expect } from '@playwright/test';

// Standalone accept-invite flow (App.tsx detects `?invite=<token>` in the URL
// at mount — see the `inviteToken` state + early `if (inviteToken)` return —
// and renders AcceptInviteView in place of the normal login/dashboard flow).
//
// There is no backend in the e2e CI job (see .github/workflows/ci.yml's
// "Frontend (build · e2e)" job — no docker services), so a real invite token
// can never be redeemed server-side here. What IS deterministically
// verifiable in this environment:
//   1. The `?invite=` routing/detection itself (renders AcceptInviteView, not
//      the login screen) — no network involved.
//   2. Submitting a valid-shaped form degrades gracefully (an error message
//      is shown, not a crash/blank page) when the fetch to the backend fails —
//      whether that's a network error (nothing listening on :8100) or an HTTP
//      error response, per AcceptInviteView's catch block.
//   3. Client-side password validation, which never touches the network.

const INVITE_URL = '/?invite=faketoken123';
const ACCEPT_INVITE_ENDPOINT = /\/v1\/users\/accept-invite$/;

// The error box (rendered only when `error` is set) is the DOM node directly
// following the confirm-password input in AcceptInviteView's JSX — a stable
// selector that doesn't depend on the exact copy shown.
const errorBox = (page: import('@playwright/test').Page) => page.locator('#accept-confirm + div');

test('navigating to a ?invite= link renders the standalone accept-invite view, not login', async ({ page }) => {
  // domcontentloaded (not the default 'load') so this doesn't hang on the
  // external Google Fonts stylesheet in index.html — the assertion below
  // already waits for the React-rendered heading itself.
  await page.goto(INVITE_URL, { waitUntil: 'domcontentloaded' });

  await expect(page.getByRole('heading', { name: /choose a password/i })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: /sign in to the dashboard/i })).toHaveCount(0);
});

test('submitting a valid-shaped invite form surfaces a graceful error (no backend in this env)', async ({ page }) => {
  await page.goto(INVITE_URL, { waitUntil: 'domcontentloaded' });

  await page.locator('#accept-password').fill('CorrectHorse9');
  await page.locator('#accept-confirm').fill('CorrectHorse9');
  await page.locator('form button[type="submit"]').click();

  // Whether the fetch to the (unreachable) backend fails as a network error or
  // resolves to an HTTP error, AcceptInviteView's catch block always sets a
  // non-empty error message and stays on this screen — it must never crash or
  // go blank. Assert loosely on the error box itself rather than exact copy.
  await expect(errorBox(page)).toBeVisible({ timeout: 15_000 });
  await expect(errorBox(page)).not.toHaveText('');

  // Still on the accept-invite screen — a failed submit does not navigate away.
  await expect(page.getByRole('heading', { name: /choose a password/i })).toBeVisible();
});

test('client-side password validation blocks submission before any network call', async ({ page }) => {
  let acceptInviteRequests = 0;
  await page.route(ACCEPT_INVITE_ENDPOINT, (route) => {
    acceptInviteRequests += 1;
    return route.abort();
  });

  await page.goto(INVITE_URL, { waitUntil: 'domcontentloaded' });

  // Too short (both fields, so it can't also fail as a mismatch).
  await page.locator('#accept-password').fill('short1');
  await page.locator('#accept-confirm').fill('short1');
  await page.locator('form button[type="submit"]').click();
  await expect(errorBox(page)).toHaveText('Password must be at least 8 characters.');

  // Long enough, but mismatched.
  await page.locator('#accept-password').fill('CorrectHorse9');
  await page.locator('#accept-confirm').fill('DifferentHorse9');
  await page.locator('form button[type="submit"]').click();
  await expect(errorBox(page)).toHaveText('Passwords do not match.');

  expect(acceptInviteRequests).toBe(0);
});
