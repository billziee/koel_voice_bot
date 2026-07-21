import { test, expect, type Page } from '@playwright/test';

// Admin nav gating + AdminView CRUD — no live backend required.
//
// Two independent, backend-free paths are covered here:
//
//  1. Offline demo login (same fallback dashboard.spec.ts exercises): a
//     network error during login makes LoginView fall back to
//     onLogin({ live: false, token: null }) — no JWT at all. App.tsx computes
//     `isAdmin = token ? tokenRole(token) === 'admin' : false`, so with a null
//     token isAdmin is necessarily false. This is a real regression test on
//     that role gate: it fails if the Admin nav item is ever shown without a
//     token.
//
//  2. A mocked "live" login (same technique as auth-persistence.spec.ts):
//     App.tsx/koel.ts only base64-decode the JWT payload to read `role` /
//     `org_id` — the signature is never verified client-side (the backend is
//     the real authority; see the "UI gating only" comments in App.tsx and
//     koel.ts) — so an unsigned token with role: 'admin' is enough to flip
//     `isAdmin` true and reach the real AdminView. `/v1/orgs` and `/v1/users`
//     are fully faked with page.route(), so the actual CRUD UI (list orgs,
//     create an org, deactivate an org, list users) is exercised end-to-end
//     with no backend at all.

// Sign in via the demo credentials. Works unmodified for both the offline
// demo path (no routes mocked — /v1/auth/login fails to connect and falls
// back to demo mode) and the mocked-live path (a page.route() intercepts
// /v1/auth/login before any real network call happens). The "I'm human" gate
// is a simulated Turnstile: clicking the checkbox fires onChange, which swaps
// the input out for an ~800ms "Verifying…" spinner and only then flips to
// "Verified". submit() rejects while humanVerified is still false, so we must
// click the checkbox (not .check() — the input detaches, so .check() can
// never confirm its checked state) and wait for "Verified" before pressing
// Sign In.
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

// A well-formed but unsigned JWT. The FE never verifies the signature (only
// decodes the payload to read role/org_id/exp), so this is enough to
// deterministically drive the role gate and the AdminView platform-admin
// branch without a real auth server.
function fakeJwt(claims: Record<string, unknown>): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const exp = Math.floor(Date.now() / 1000) + 900; // 15-min access TTL
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'admin-e2e', exp, ...claims })}.sig`;
}

interface MockOrg { id: string; name: string; slug: string; active: boolean; created_at: string | null }
interface MockUser {
  id: string; username: string | null; email: string | null; org_id: string | null;
  role: 'operator' | 'admin'; active: boolean; invite_pending: boolean; created_at: string | null;
}

test.describe('Admin nav gating (offline demo — no backend, no token)', () => {
  test('the Admin nav item is absent for a non-admin/no-token session', async ({ page }) => {
    await signIn(page);
    await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });

    // Real regression coverage on App.tsx's role gate: the offline-demo
    // fallback token is null, so `isAdmin` is always false and the "Admin"
    // nav button (rendered only when isAdmin) must not exist at all.
    await expect(page.getByRole('button', { name: /^admin$/i })).toHaveCount(0);
  });
});

test.describe('Admin panel (mocked-live platform-admin session)', () => {
  let orgs: MockOrg[];
  let users: MockUser[];

  test.beforeEach(async ({ page }) => {
    orgs = [{ id: 'org-1', name: 'Acme Bank', slug: 'acme', active: true, created_at: null }];
    users = [
      { id: 'user-1', username: 'jane', email: 'jane@acme.com', org_id: 'org-1', role: 'operator', active: true, invite_pending: false, created_at: null },
    ];

    // Regexes (matched against the full URL) rather than glob patterns, since
    // the FE calls an absolute origin (http://localhost:8100) — see
    // auth-persistence.spec.ts's mockBackend for the same approach.
    await page.route(/\/v1\/auth\/login$/, (route) =>
      route.fulfill({ json: { access_token: fakeJwt({ role: 'admin', org_id: null }) } }),
    );
    // Dashboard hydration after login (0 bots/stats is fine — we only need the
    // dashboard to render so the Admin nav item is reachable).
    await page.route(/\/v1\/bots$/, (route) => route.fulfill({ json: [] }));
    await page.route(/\/v1\/stats$/, (route) => route.fulfill({ json: [] }));

    await page.route(/\/v1\/orgs$/, (route) => {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON() as { name: string; slug: string };
        const created: MockOrg = { id: `org-${orgs.length + 1}`, name: body.name, slug: body.slug, active: true, created_at: null };
        orgs = [...orgs, created];
        return route.fulfill({ json: created });
      }
      return route.fulfill({ json: orgs });
    });

    await page.route(/\/v1\/orgs\/[^/]+\/deactivate$/, (route) => {
      const id = new URL(route.request().url()).pathname.split('/')[3];
      orgs = orgs.map((o) => (o.id === id ? { ...o, active: false } : o));
      const updated = orgs.find((o) => o.id === id);
      return route.fulfill({ json: updated });
    });

    await page.route(/\/v1\/users(\?.*)?$/, (route) => route.fulfill({ json: users }));

    await signIn(page);
    await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: /^admin$/i }).click();
    // AdminView is a React.lazy chunk — give the dynamic import time to resolve
    // (mirrors dashboard.spec.ts's wait for the BotBuilderView chunk).
    await expect(page.getByRole('heading', { name: /^admin$/i })).toBeVisible({ timeout: 10_000 });
  });

  test('lists organizations and users loaded from the mocked backend', async ({ page }) => {
    await expect(page.getByText(/Organizations \(1\)/)).toBeVisible();
    await expect(page.getByText('Acme Bank', { exact: true })).toBeVisible();
    await expect(page.getByText('acme', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Users', exact: true }).click();
    await expect(page.getByText(/Users \(1\)/)).toBeVisible();
    await expect(page.getByText('jane@acme.com')).toBeVisible();
  });

  test('creates a new organization through the New org form', async ({ page }) => {
    await page.getByRole('button', { name: /new org/i }).click();
    await page.getByLabel('Name').fill('New Bank');
    await page.getByLabel('Slug').fill('newbank');
    await page.getByRole('button', { name: 'Create', exact: true }).click();

    await expect(page.getByText(/Organization .*created/i)).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText(/Organizations \(2\)/)).toBeVisible();
    await expect(page.getByText('New Bank', { exact: true })).toBeVisible();
  });

  test('deactivates an organization', async ({ page }) => {
    // Scope to the specific org-row container (not just any ancestor div that
    // happens to contain the name text) so the Deactivate button — a sibling
    // of the name, not a descendant of it — is actually reachable.
    const orgRow = page.locator('div.p-4.flex.items-center.justify-between', { hasText: 'Acme Bank' });
    await orgRow.getByRole('button', { name: 'Deactivate' }).click();

    await expect(page.getByText(/Organization .*deactivated/i)).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('Inactive')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Deactivate' })).toHaveCount(0);
  });
});
