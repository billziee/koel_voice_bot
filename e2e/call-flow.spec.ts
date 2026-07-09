import { test, expect, type Page } from '@playwright/test';

// Call-flow safety net (roadmap Q-1). Locks in ActiveCallView's demo-mode
// behaviour before the component is decomposed / a useLiveCall hook is split
// out: launching a bot opens the call view, and the guided demo drives the
// scripted scenario (greeting → live state) entirely client-side — no backend,
// WS, or mic needed (mic is live-mode only). Assertions target stable UI, not
// timing, so the extraction can be verified against real behaviour.

// Headless Chromium has no audio device, so the demo's Web Audio calls otherwise
// throw and stall startCall before the scripted turns run. Stub the audio APIs
// (the demo is timer-driven, not audio-event-driven) so the scenario progresses.
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

test('launching a bot opens the call view with call controls', async ({ page }) => {
  await signIn(page);
  await page.getByText('ICICI Pre-Due Collections').first().click();

  // ActiveCallView is up with its idle controls.
  await expect(page.getByRole('heading', { name: /ICICI Pre-Due Collections/i })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole('button', { name: /Start Call/i })).toBeVisible();
  await expect(page.getByRole('button', { name: /Run Guided Demo/i })).toBeVisible();
});

test('guided demo drives the scripted call: greeting bubble + live state', async ({ page }) => {
  await signIn(page);
  await page.getByText('ICICI Pre-Due Collections').first().click();

  // ActiveCallView is a React.lazy chunk — wait for its idle controls to render
  // before driving the demo (avoids racing the Suspense resolution).
  const demoButton = page.getByRole('button', { name: /Run Guided Demo/i });
  await expect(demoButton).toBeVisible({ timeout: 15_000 });

  // The idle placeholder's demo button sits under the sticky bottom control bar
  // at this viewport, so a real mouse click lands on the bar — dispatch the click
  // straight to the button to exercise its handler regardless of overlap. On
  // webkit the first dispatch can land before the lazily-loaded view settles, so
  // retry until the scripted call actually starts.
  const greeting = page.getByText(/KOEL BOT/i).first();
  await expect(async () => {
    if (await greeting.isVisible().catch(() => false)) return;
    await demoButton.dispatchEvent('click', {}, { timeout: 2_000 }).catch(() => {});
    await expect(greeting).toBeVisible({ timeout: 2_500 });
  }).toPass({ timeout: 30_000 });

  // The bot greeting streams into the transcript and the session goes live.
  await expect(greeting).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/main ICICI Bank ki taraf se KOEL/i)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: /End Call/i })).toBeVisible({ timeout: 15_000 });
});
