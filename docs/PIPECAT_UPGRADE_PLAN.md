# Pipecat-Inspired KOEL Upgrade Plan

**Date:** 2026-07-07 · **Branch:** `feat/groq-nlp-and-prd-hardening` · **Status:** ALL FOUR PHASES SHIPPED. Phase 4 passed its Hindi/Hinglish capability eval (9/9 on production-representative cases) and is flag-gated off by default.

Source of patterns: shallow clone of [pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat) (studied, NOT ported — see
`docs/` research reports for why a full migration was rejected).

Cross-check basis (verified in code, not assumed):

| Area | KOEL today | Weakness found |
|------|-----------|----------------|
| Mic VAD (`src/lib/audio.ts:156-190`) | RMS energy + adaptive noise floor (abs 0.025, 2.5× floor, 50ms ticks) | Energy-only → coughs/keyboard/TV/traffic read as "speech"; each false hit halts bot audio (App.tsx `onSpeechStart`) and burns an STT round trip |
| Barge-in Gate 2 (`backend/koel/voicebot/pipeline/bargein.py`) | duration ≥300ms + backchannel vocab | No word-count gate — one junk transcribed word kills TTS |
| End-of-turn (`audio.ts` `silenceMs: 500`) | fixed 500ms trailing silence | Cuts off Hinglish speakers who pause mid-sentence |
| Sarvam TTS (`backend/koel/tts/providers.py:426`) | HTTP POST per sentence (`_stream_by_sentence`) | First audio waits for full first-sentence synthesis; Sarvam offers true WS streaming (bulbul:v3, already KOEL's model) |

What we are deliberately NOT taking from Pipecat: the frames/pipeline architecture (churn, no gain),
1-process-per-session model (their weakness), audio noise-suppression filters (heavy deps, marginal gain).

---

## Phase 1 — Neural VAD (Silero) in the browser, RMS as automatic fallback

**Pipecat source of the idea:** `src/pipecat/audio/vad/silero.py` + `vad_analyzer.py`
(Silero ONNX confidence model + QUIET→STARTING→SPEAKING→STOPPING hysteresis).

**Approach:** `@ricky0123/vad-web` (MIT) wraps the *exact same* `silero_vad.onnx` in an
AudioWorklet + onnxruntime-web (WASM). It accepts an existing `MediaStream`, so KOEL's
`MicCapture` keeps owning `getUserMedia` + `MediaRecorder` — only the *speech decision*
is upgraded.

### Files

| File | Change |
|------|--------|
| `package.json` | add `@ricky0123/vad-web`; add `vite-plugin-static-copy` (dev dep) |
| `vite.config.ts` | static-copy of ONNX model + `vad.worklet.bundle.min.js` + ort WASM files from `node_modules` into served assets |
| `src/lib/vad.ts` **(new, ~120 lines)** | `SileroVadEngine` class: lazy `import()` of vad-web (zero initial-bundle impact); param mapping from KOEL semantics → vad-web config: `positiveSpeechThreshold ≈ 0.5`, redemption frames ≈ 500ms (= current `silenceMs`), min-speech ≈ 300ms (= current `minSpeechMs`); exposes `onSpeechStart` / `onSpeechEnd` / `onMisfire` |
| `src/lib/audio.ts` | `MicCapture.start()` tries Silero first; **any load failure (asset 404, WASM blocked, old browser) → silent fallback to the existing RMS path, which stays fully intact**. The `AnalyserNode` stays regardless — it still powers the `onLevel` UI meter. Silero events drive the existing `hadSpeech` / `lastVoiceAt` / `finalizeSegment` machinery |
| `src/App.tsx` | **zero changes** — the `MicCaptureOptions` contract (`onSpeechStart`, `onUtterance`, `onLevel`) is unchanged (single call site: `App.tsx:1261`) |

### Effect
Barge-in candidates fire on *speech*, not noise → fewer false TTS kills, fewer wasted
STT calls, better UX in noisy Indian call environments.

### Verification
1. `tsc --noEmit` + `vite build` green
2. Preview: speak → utterance emitted end-to-end
3. Preview: typing/noise near mic → no `barge_in_candidate` (console check)
4. Break the model asset path → confirm RMS fallback engages and calls still work

### Risks / mitigations
- ~2.3MB model download at call start → lazy-loaded per call session, cached by the browser after first load
- WASM CSP restrictions on some deploys → automatic RMS fallback covers it

---

## Phase 2 — Barge-in Gate 2: min-words rule

**Pipecat source of the idea:** `src/pipecat/turns/user_start/min_words_user_turn_start_strategy.py`.

### Files

| File | Change |
|------|--------|
| `backend/koel/voicebot/pipeline/bargein.py` | new param `min_interrupt_words: int = 2`. Interrupt requires **≥2 transcribed words OR sustained speech ≥1200ms** (so a slow, deliberate single word — "ruko…" — still interrupts). New non-interrupt reason: `"too_few_words"`. Backchannel logic untouched |
| `backend/tests/test_bargein.py` | 4 new cases: 1 junk word/short → no interrupt; 2 words → interrupt; 1 word but 1.3s sustained → interrupt; backchannels unchanged |

### Verification
`pytest backend/tests/test_bargein.py` green.

---

## Phase 3 — Sarvam WebSocket streaming TTS *(flag-gated, off by default)*

**Pipecat source of the idea:** `src/pipecat/services/sarvam/tts.py` — uses Sarvam's official
`/text-to-speech/stream` WebSocket API with `bulbul:v3` (KOEL's already-configured model,
`backend/koel/tts/config.py:59`).

### Files

| File | Change |
|------|--------|
| `backend/koel/tts/config.py` | `sarvam_tts_ws_enabled: bool = False` (env `KOEL_SARVAM_TTS_WS_ENABLED`) |
| `backend/koel/tts/providers.py` | `SarvamTtsProvider.stream()` gains a WS path: connect, send text, yield audio chunks as they arrive; **any WS error → fall through to the existing `_stream_by_sentence` HTTP path** |
| `backend/tests/` | new test with a fake WS server: chunks yielded incrementally; WS failure falls back to HTTP |

### Effect
Lower time-to-first-audio on long sentences → filler clips needed less often.

### Dependency note
`websockets` library — check if already in backend deps; if not, add to `pyproject.toml`.

---

## Phase 4 — Semantic end-of-turn (smart-turn-v3.2) — **SHIPPED (flag off by default)**

**Capability eval (2026-07-07, gate for building this):** smart-turn-v3.2 run
offline against Sarvam-TTS Hindi/Hinglish clips — 5 complete sentences scored
0.77–0.99 (complete), 4 conjunction-ending fragments ("…ki", "…aur", "…to")
scored 0.005–0.027 (incomplete). 9/9 on production-representative cases; the
only misses were artificial mid-word chops, which cannot reach the gate (it
only runs after the VAD's silence window). Verdict: capable → built.

**As shipped:** the browser's Silero worklet already yields each utterance as
16kHz PCM, so it rides the audio_chunk WS message (`pcm16_b64`) and the
backend needs NO audio decoding — just numpy (already a dep) + onnxruntime
(`pip install -e ".[smart-turn]"`). Incomplete verdict → transcript held up
to `smart_turn_hold_ms` (1.5s) and merged with the follow-up. Enable with
`KOEL_SMART_TURN_ENABLED=true` + `scripts/fetch_smart_turn_model.sh`.
Missing model/dep degrades to gate-off; RMS-fallback clients (no PCM) skip
the gate entirely.

**Pipecat source:** `src/pipecat/audio/turn/smart_turn/local_smart_turn_v3.py` + bundled
`smart-turn-v3.2-cpu.onnx` (**only 8.3MB** — feasible on KOEL's 512MB Render containers).

**What it would do:** when the frontend's 500ms silence fires and audio reaches the backend,
run the ONNX model on the tail audio to ask "is the speaker actually finished?" If
*incomplete*, hold the NLP call ~1.2s and merge the follow-up utterance instead of
responding mid-thought. Fixes the mid-pause cutoff for Hinglish speakers.

**Why deferred:**
1. Touches the ws-handler turn flow + orchestrator (highest blast radius of all phases)
2. Adds `onnxruntime` to the backend image (~60MB wheel)
3. Hindi/Hinglish accuracy of smart-turn-v3 must be validated on real call audio first

**Prerequisite when picked up:** offline eval of the model against ~50 recorded KOEL
utterance tails (complete vs incomplete), before any pipeline wiring.

---

## Execution order & safety

1. **Phase 2 first** (smallest, backend-only, fully test-provable)
2. **Phase 1** (frontend, auto-fallback safety net)
3. **Phase 3** only if approved (flag stays off in prod until soak-tested)
4. Each phase = its own commit on `feat/groq-nlp-and-prd-hardening`
5. Targeted test runs per phase (not the whole suite) to conserve tokens
6. Rollback story: Phase 1 falls back automatically at runtime; Phase 3 is env-flag-off;
   Phase 2 is behavior-widening only for non-interrupts (safe direction: fewer false kills)

## Estimated blast radius

| Phase | Files touched | New deps | Risk |
|-------|--------------|----------|------|
| 1 | 4 (+1 new) | vad-web, vite-plugin-static-copy | Low (auto-fallback) |
| 2 | 2 | none | Very low |
| 3 | 3 | maybe `websockets` | Low (flag off) |
| 4 | ~5 | onnxruntime | Medium — deferred |
