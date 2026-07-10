import { test, expect, type Page } from '@playwright/test';

// Q-7 — dynamic edge-simulation e2e. ActiveCallView runs entirely backend-free
// in demo mode: the guided demo and the "simulation deck" chips drive scripted
// edge cases on deterministic client-side timers (no mic / WebSocket / backend),
// so their milestones are stable to assert — unlike a live audio/socket path.
// Assertions target stable text/role signals with generous timeouts and no
// time-based sleeps. The signIn + launch helpers mirror e2e/call-flow.spec.ts
// (which must not be edited) so the sandbox is reached the same proven way.

// Headless browsers have no audio device, so the demo's Web Audio calls would
// otherwise throw and stall startCall before the scripted turns run. Stub the
// audio APIs (the simulations are timer-driven, not audio-event-driven) so the
// scenarios progress. Copied from call-flow.spec.ts.
async function stubAudio(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FakeAudioContext {
      state = 'running';
      currentTime = 0;
      destination = {};
      createGain() { return { gain: { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {}, disconnect() {} }; }
      createBufferSource() { return { buffer: null, connect() {}, start() {}, stop() {}, onended: null }; }
      createMediaElementSource() { return { connect() {}, disconnect() {} }; }
      createAnalyser() { return { fftSize: 2048, frequencyBinCount: 1024, smoothingTimeConstant: 0, getByteFrequencyData() {}, getByteTimeDomainData() {}, connect() {}, disconnect() {} }; }
      decodeAudioData() { return Promise.resolve({ duration: 0 }); }
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
    }
    // @ts-expect-error test stub
    window.AudioContext = FakeAudioContext;
    // @ts-expect-error test stub
    window.webkitAudioContext = FakeAudioContext;
    HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  });
}

async function signIn(page: Page): Promise<void> {
  await stubAudio(page);
  await page.goto('/');
  await page.locator('#login-email').fill('demo');
  await page.locator('#login-password').fill('koel-demo');
  const human = page.getByRole('checkbox', { name: /human/i });
  if (await human.count()) {
    await human.click();
    await expect(page.getByText('Verified', { exact: true })).toBeVisible({ timeout: 5_000 });
  }
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
}

// Open the sandbox call view for the ICICI demo bot WITHOUT auto-running the
// guided demo, so a test can drive Start Call + individual simulation chips
// itself. The lazy view is settled once its idle Start Call control renders.
async function openSandbox(page: Page): Promise<void> {
  await page.getByText('ICICI Pre-Due Collections').first().click();
  await expect(page.getByRole('button', { name: /Start Call/i })).toBeVisible({ timeout: 15_000 });
}

// 1) Barge-in — the "Barge-In (interrupt)" chip is enabled only while the bot is
// speaking (the greeting window); clicking it runs simulateBargeIn, which marks
// the most recent bot bubble interrupted → a persistent "interrupted" badge
// (TranscriptMessage). click() auto-waits until the chip is actionable, so it
// lands mid-greeting without a sleep. (Driving this via the guided demo is
// unreliable: the ~12s ICICI greeting outlasts the demo's ~9.5s barge-in step,
// so the no-input watchdog closes the call before barge-in fires.)
test('the barge-in chip flags the interrupted transcript bubble', async ({ page }) => {
  test.setTimeout(90_000);
  await signIn(page);
  await openSandbox(page);
  await page.getByRole('button', { name: /Start Call/i }).click();

  // Wait for the greeting bubble first so there's a bot turn to interrupt — the
  // chip enables at bot_speaking (~1.2s) but the greeting isn't added until ~2.3s,
  // and the ~12s greeting leaves plenty of speaking window to still barge in.
  await expect(page.getByText(/KOEL BOT/i).first()).toBeVisible({ timeout: 30_000 });
  await page.getByRole('button', { name: /Barge-In \(interrupt\)/i }).click({ timeout: 45_000 });

  await expect(page.getByText('interrupted', { exact: true }).first()).toBeVisible({ timeout: 30_000 });
});

// 2) Escalation → agent handoff. Once the bot finishes greeting the call goes
// to 'listening' and the scripted reply chips render. The "This bill is wrong"
// chip carries action ESCALATE, which drives callState to 'escalating' —
// rendering the handoff line and the AgentHandoffPanel overlay. click() auto-
// waits until the chip is actionable (it only renders in the listening window),
// so no sleep is needed.
test('a scripted escalation chip hands off to the agent queue', async ({ page }) => {
  test.setTimeout(90_000);
  await signIn(page);
  await openSandbox(page);
  await page.getByRole('button', { name: /Start Call/i }).click();

  await page.getByRole('button', { name: /This bill is wrong/i }).click({ timeout: 45_000 });

  await expect(page.getByText(/Handing off to agent queue with full context/i)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Agent Console')).toBeVisible({ timeout: 10_000 });
});

// 3) Network drop → reconnect. The "Drop Network" EdgeChip is enabled once the
// session is live (bot_speaking onward). simulateNetworkDrop flips callState to
// 'reconnecting' and arms the 15s grace-TTL recovery UI. click() auto-waits
// until the chip is enabled. The reconnecting status badge is the transient
// state signal; the dropped-socket meta bubble persists in the transcript as a
// backstop that the drop actually happened.
test('dropping the network drives the session into the reconnecting state', async ({ page }) => {
  test.setTimeout(90_000);
  await signIn(page);
  await openSandbox(page);
  await page.getByRole('button', { name: /Start Call/i }).click();

  await page.getByRole('button', { name: /Drop Network/i }).click({ timeout: 30_000 });

  await expect(page.getByText(/Reconnecting/i).first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/WebSocket dropped/i).first()).toBeVisible({ timeout: 10_000 });
});
