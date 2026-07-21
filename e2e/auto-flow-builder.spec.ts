import { test, expect, type Page } from '@playwright/test';

// Regression coverage for the Bot Builder step-order fix: "Knowledge & CRM"
// (where the RAG index is set) now comes BEFORE "Conversation flow" (where the
// "Auto-build from knowledge base" button lives) — see the BUILDER_STEPS
// comment in src/views/BotBuilderView.tsx. Before the fix, Knowledge & CRM was
// one step AFTER Conversation flow, so a first-time, linear pass through the
// wizard reached the auto-build button with an empty ragIndex and the button
// never appeared.
//
// The button's `onAutoBuild` prop (BotBuilderView.tsx) is only defined when
// BOTH `token` is truthy AND `ragIndex.trim()` is non-empty:
//   token && ragIndex.trim() ? () => generateFlow(...) : undefined
// FlowEditor only renders the button at all when `onAutoBuild` is defined —
// `{onAutoBuild && (<button>…</button>)}` — so an absent token means the
// button is not merely disabled, it does not exist in the DOM.
//
// The plain offline-demo path (see dashboard.spec.ts's signIn) always yields
// `token: null` — LoginView's `submit()` falls back to
// `onLogin({ live: false, token: null })` on any network failure, and nothing
// listens on the backend origin in CI. That means the button can NEVER render
// under the pure offline path regardless of ragIndex, so it cannot be used to
// regression-test the step-order fix on its own.
//
// To actually exercise the token-gated branch without a live backend, /v1/auth
// /login is mocked to return a well-formed-but-unsigned JWT — the same
// technique already used by auth-persistence.spec.ts and admin-view.spec.ts.
// The FE only base64-decodes the JWT payload client-side (never verifies the
// signature; the backend is the real authority — see koel.ts), so this is
// enough to make `token` truthy and reach the real onAutoBuild branch. We never
// click the button: clicking would call generateFlow(), a real POST this mock
// does not (and should not) answer.

function fakeJwt(claims: Record<string, unknown> = {}): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const exp = Math.floor(Date.now() / 1000) + 900; // 15-min access TTL
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'e2e-user', role: 'operator', exp, ...claims })}.sig`;
}

// Mocks just enough of the backend for a "live" (token-bearing) session to
// reach the dashboard and the builder. /v1/knowledge/generate-flow is
// deliberately NOT mocked — the tests below never click the auto-build button,
// so that endpoint should never be hit.
async function mockBackend(page: Page): Promise<void> {
  // Regexes (matched against the full URL), not glob patterns — the FE calls
  // an absolute origin (http://localhost:8100), same approach as
  // auth-persistence.spec.ts / admin-view.spec.ts.
  await page.route(/\/v1\/auth\/login$/, (route) => route.fulfill({ json: { access_token: fakeJwt() } }));
  await page.route(/\/v1\/bots$/, (route) => route.fulfill({ json: [] }));
  await page.route(/\/v1\/stats$/, (route) => route.fulfill({ json: [] }));
  // Knowledge index list (Step 3 datalist) — degrades to free-text entry when
  // empty, which is what we exercise here (typing a brand-new index name).
  await page.route(/\/v1\/knowledge-indexes$/, (route) => route.fulfill({ json: [] }));
}

// Same "I'm human" Turnstile-simulation dance as dashboard.spec.ts's signIn.
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

async function openBuilder(page: Page): Promise<void> {
  await signIn(page);
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
  // BotBuilderView is a React.lazy chunk — wait for its heading after launching.
  await page.getByRole('button', { name: /New bot/i }).first().click();
  await expect(page.getByRole('heading', { name: /Build a new bot/i })).toBeVisible({ timeout: 10_000 });
}

test('auto-build button is enabled on step 4 once the RAG index is set in step 3', async ({ page }) => {
  await mockBackend(page);
  await openBuilder(page);

  // Step 1 (Use case) -> Step 2 (Persona & voice).
  await page.getByRole('button', { name: /Continue/i }).click();
  // Step 2 -> Step 3. Assert the step-3 heading is really "Knowledge & CRM" —
  // if the old ordering ever came back, this step would be "Conversation flow"
  // instead and the assertion below would catch it immediately.
  await page.getByRole('button', { name: /Continue/i }).click();
  await expect(page.getByRole('heading', { name: 'Knowledge & CRM', exact: true })).toBeVisible();

  // The RAG index field has no htmlFor/id linking it to its <label> (a real
  // gap in the markup), so getByLabel can't find it — target the input's
  // unique `list` attribute (the datalist it feeds) instead.
  const ragIndexInput = page.locator('input[list="kb-indexes"]');
  await ragIndexInput.fill('cc_hdfc_e2e');
  await expect(ragIndexInput).toHaveValue('cc_hdfc_e2e');

  // Step 3 -> Step 4 (Conversation flow) — the regression check.
  await page.getByRole('button', { name: /Continue/i }).click();
  await expect(page.getByRole('heading', { name: 'Conversation flow', exact: true })).toBeVisible();

  // With a live token AND a non-empty ragIndex already set, onAutoBuild is
  // defined, so the button must exist AND be enabled. We do not click it —
  // that would call the real generateFlow() network call, which this test
  // deliberately leaves unmocked.
  const autoBuildBtn = page.getByRole('button', { name: /Auto-build from knowledge base/i });
  await expect(autoBuildBtn).toBeVisible();
  await expect(autoBuildBtn).toBeEnabled();
});

test('auto-build button is absent on step 4 when no RAG index has been set yet', async ({ page }) => {
  await mockBackend(page);
  await openBuilder(page);

  // Walk straight to step 4 without ever touching the Knowledge & CRM field —
  // ragIndex stays ''. This isolates ragIndex (not just a live token) as the
  // actual gate, so the first test's pass can't be a false positive from the
  // button always being enabled once signed in.
  for (let i = 0; i < 3; i++) {
    await page.getByRole('button', { name: /Continue/i }).click();
  }
  await expect(page.getByRole('heading', { name: 'Conversation flow', exact: true })).toBeVisible();

  await expect(page.getByRole('button', { name: /Auto-build from knowledge base/i })).toHaveCount(0);
});
