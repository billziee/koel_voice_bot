import { test, expect, type Page, type WebSocketRoute } from '@playwright/test';

// O-6 Wave 2 — streaming partial-transcript UX. The REST backend is mocked so
// the FE runs in LIVE mode without a stack (mirrors auth-persistence.spec.ts),
// and page.routeWebSocket intercepts /ws/calls so a scripted server pushes a
// sequence of interim `partial_transcript` frames followed by the authoritative
// final `transcript`. The frames are driven step-by-step from the test body and
// each interim is asserted before the next is pushed, so the sequence is
// deterministic — no arbitrary sleeps, only expect(...) auto-retry polling.

// A well-formed (unsigned) JWT with a future `exp`. The FE only base64-decodes
// the payload to schedule refresh — it never verifies the signature — so this is
// enough to drive the live path. (Same trick as auth-persistence.spec.ts.)
function fakeJwt(expEpochSec: number): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'demo', role: 'operator', exp: expEpochSec })}.sig`;
}

// One live bot in streaming (config.stt_mode='ws') mode. backendBotToBot hydrates
// backendId + sttMode from this, so useLiveCall runs the real live-call path and
// the mic pipeline forwards continuous frames.
const WS_BOT = {
  id: 'bot_ws_1',
  name: 'Streaming STT Bot',
  use_case: 'collections',
  language: 'hi',
  default_tone: 'firm',
  rag_enabled: false,
  rag_index: null,
  active: true,
  config: { stt_mode: 'ws' },
};

const SESSION = {
  session_id: 'sess_e2e_stream',
  ws_token: 'wstok_e2e',
  ws_url: 'ws://localhost:8100/ws/calls/sess_e2e_stream',
  crm_context: {},
  language: 'hi',
  use_case: 'collections',
};

// Regexes match against the full URL — the FE calls an absolute origin
// (http://localhost:8100), so path globs would miss.
async function mockBackend(page: Page): Promise<void> {
  const future = Math.floor(Date.now() / 1000) + 900;
  await page.route(/\/v1\/auth\/login$/, (r) => r.fulfill({ json: { access_token: fakeJwt(future) } }));
  await page.route(/\/v1\/auth\/refresh$/, (r) => r.fulfill({ json: { access_token: fakeJwt(future) } }));
  await page.route(/\/v1\/bots$/, (r) => r.fulfill({ json: [WS_BOT] }));
  await page.route(/\/v1\/stats$/, (r) => r.fulfill({ json: [] }));
  await page.route(/\/v1\/sessions$/, (r) => r.fulfill({ json: SESSION }));
}

// Headless browsers have no audio device. Stub Web Audio + getUserMedia +
// MediaRecorder so (a) the demo/live audio graph never throws, and (b)
// micSupported() is true and arming the mic succeeds with a fake device — no
// real hardware needed. The stubs are inert (no events fire); the scripted WS
// drives all observable behaviour.
async function stubMedia(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const fakeNode = () => ({
      connect() {}, disconnect() {}, gain: { value: 0, setValueAtTime() {}, linearRampToValueAtTime() {} },
      onaudioprocess: null, buffer: null, start() {}, stop() {}, onended: null,
    });
    class FakeAudioContext {
      state = 'running';
      currentTime = 0;
      sampleRate = 48000;
      destination = {};
      createGain() { return fakeNode(); }
      createBufferSource() { return fakeNode(); }
      createMediaElementSource() { return fakeNode(); }
      createMediaStreamSource() { return fakeNode(); }
      createScriptProcessor() { return fakeNode(); }
      createAnalyser() {
        return {
          fftSize: 2048, frequencyBinCount: 1024, smoothingTimeConstant: 0,
          getByteFrequencyData() {}, getByteTimeDomainData() {}, connect() {}, disconnect() {},
        };
      }
      decodeAudioData() { return Promise.resolve({ duration: 0 }); }
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
    }
    // @ts-expect-error test stub
    window.AudioContext = FakeAudioContext;
    // @ts-expect-error test stub
    window.webkitAudioContext = FakeAudioContext;
    HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };

    // Fake capture stream + recorder so the mic path arms without hardware.
    const fakeStream = {
      getTracks: () => [{ stop() {}, kind: 'audio' }],
      getAudioTracks: () => [{ stop() {}, kind: 'audio' }],
      getVideoTracks: () => [],
    };
    const md = (navigator.mediaDevices || {}) as MediaDevices;
    // @ts-expect-error override for test
    md.getUserMedia = async () => fakeStream;
    if (!navigator.mediaDevices) {
      Object.defineProperty(navigator, 'mediaDevices', { value: md, configurable: true });
    }
    class FakeMediaRecorder {
      static isTypeSupported() { return true; }
      state = 'inactive';
      ondataavailable: ((e: unknown) => void) | null = null;
      onstop: (() => void) | null = null;
      start() { this.state = 'recording'; }
      stop() { this.state = 'inactive'; this.onstop?.(); }
      requestData() {}
    }
    // @ts-expect-error test stub
    window.MediaRecorder = FakeMediaRecorder;
  });
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
  await expect(page.getByText(/Bot Dashboard|Dashboard/i).first()).toBeVisible({ timeout: 15_000 });
}

// Open the live sandbox for the streaming bot and Start Call — this triggers
// POST /v1/sessions (mocked) then the LiveCall WebSocket (routed). The lazy view
// is settled once its idle Start Call control renders.
async function enterLiveCall(page: Page): Promise<void> {
  await page.getByText('Streaming STT Bot').first().click();
  await expect(page.getByRole('button', { name: /Start Call/i })).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: /Start Call/i }).click();
}

const frame = (obj: Record<string, unknown>) => JSON.stringify(obj);

test('live interim partials render in sequence, then are superseded by the final transcript', async ({ page }) => {
  test.setTimeout(60_000);

  await mockBackend(page);
  await stubMedia(page);

  // Capture the intercepted socket so the test can push scripted frames one at a
  // time (accepting — never forwarding to — the client's frames).
  let resolveWs!: (ws: WebSocketRoute) => void;
  const wsReady = new Promise<WebSocketRoute>((res) => { resolveWs = res; });
  await page.routeWebSocket(/\/ws\/calls\//, (ws) => {
    ws.onMessage(() => { /* swallow client audio_chunk / end_call frames */ });
    resolveWs(ws);
  });

  await signIn(page);
  await enterLiveCall(page);

  const ws = await wsReady;
  // Put the call into a live listening state (drives the status/UI as a real call).
  ws.send(frame({ type: 'call_state', data: { status: 'listening' } }));

  const caption = page.getByTestId('partial-caption');

  // Interim #1 — a partial appears.
  ws.send(frame({ type: 'partial_transcript', data: { speaker: 'customer', text: 'I want' } }));
  await expect(caption).toHaveText('I want');

  // Interim #2 — the SAME caption is revised in place (not a new line).
  ws.send(frame({ type: 'partial_transcript', data: { speaker: 'customer', text: 'I want to know' } }));
  await expect(caption).toHaveText('I want to know');
  await expect(caption).toHaveCount(1);

  // Interim #3.
  ws.send(frame({ type: 'partial_transcript', data: { speaker: 'customer', text: 'I want to know more about the offer' } }));
  await expect(caption).toHaveText('I want to know more about the offer');

  // Authoritative final transcript for the turn — supersedes the live caption:
  // the caption is removed and a settled Customer transcript bubble takes over.
  ws.send(frame({ type: 'transcript', data: { speaker: 'customer', text: 'I want to know more about the offer', index: 0 } }));
  await expect(caption).toHaveCount(0);
  await expect(
    page.getByText('I want to know more about the offer', { exact: true }),
  ).toBeVisible({ timeout: 10_000 });
});

test('streaming mic arms with a fake device and the live-call surface is available', async ({ page, browserName }) => {
  test.setTimeout(60_000);
  // Headless WebKit doesn't drive the stubbed getUserMedia → MediaRecorder capture
  // path to the "Listening…" state (the fake-media-stream support is Chromium/Firefox
  // only); the ws-mode mic pipeline itself is covered there. Skip to keep CI honest.
  test.skip(browserName === 'webkit', 'fake mic capture unsupported under headless WebKit');

  await mockBackend(page);
  await stubMedia(page);
  await page.routeWebSocket(/\/ws\/calls\//, (ws) => {
    ws.onMessage(() => {});
    ws.send(frame({ type: 'call_state', data: { status: 'listening' } }));
  });

  await signIn(page);
  await enterLiveCall(page);

  // The Mic control renders only in live mode when the browser supports capture
  // (micSupported()), which the stubs satisfy. Arming it exercises the ws-mode
  // mic pipeline (continuous-frame path wired) without real hardware.
  const mic = page.getByRole('button', { name: /^Mic$/i });
  await expect(mic).toBeVisible({ timeout: 15_000 });
  await mic.click();
  await expect(page.getByRole('button', { name: /Listening…/i })).toBeVisible({ timeout: 15_000 });
});
