# KOEL — Telephony (µ-law seam built; PSTN bridge documented)

The orchestrator is transport-agnostic — it drives a call through a `send`
callback, so the same pipeline serves the browser WebSocket today and a PSTN leg
later. Two layers:

## Built now — the µ-law seam (F)

- `SynthesizeRequest.telephony` + `KOEL_TELEPHONY_MODE` gate post-mix transcoding.
- `koel.tts.mixer.to_ulaw_8k` converts the 24 kHz WAV to raw 8 kHz mono G.711
  µ-law (PCMU), applied at the `RoutingTtsProvider` choke point so every engine's
  output is transcoded uniformly. Default **off** — browser calls stay 24 kHz WAV.
- The grace-TTL dropped-call finalizer is WS-transport-only, so a long PSTN leg
  won't be mis-finalized as `call_dropped` (see the handler's disconnect path).

## Not built — the PSTN Media Streams bridge (U, post-launch)

A separate bridge service terminates the carrier connection and reuses the
orchestrator via its `send` callback:

1. **Carrier**: Twilio / Exotel **Media Streams** — bidirectional 8 kHz µ-law
   over a WebSocket. The bridge decodes inbound µ-law → PCM for STT, and encodes
   the orchestrator's WAV → µ-law (the seam above) for the outbound leg.
2. **Outbound dialer / campaign**: DND scrub before dial (already enforced at
   session start), RBI calling-hours scheduler (partial — `enforce_calling_hours`),
   retry scheduling on `busy`/`no_answer`, and a per-campaign concurrency cap.
3. **Escalation**: warm transfer to a human via the carrier's TaskRouter/queue;
   the bot mutes ambient and hands off (escalation path already exists).
4. **DTMF**: capture keypad input for IVR-style flows (menu, OTP entry).
5. **Barge-in over PSTN**: energy-VAD on the inbound µ-law stream drives the same
   two-gate barge-in classifier used for the browser mic.

### Integration points (already present)

- Orchestrator `send` callback → the bridge writes frames to the carrier socket.
- `SynthesizeRequest.telephony=True` → µ-law output.
- STT accepts an `audio_format` → feed `ulaw`/PCM frames from the carrier.
- Session lifecycle, resilience, compliance, and RAG are transport-independent.

### Explicitly out of scope this phase

No Twilio credentials, webhook signature validation, or Media Streams WS server
are wired. When built, follow the `twilio-voice-conversation-relay` /
`twilio-taskrouter-routing` patterns and validate carrier webhooks per
`twilio-webhook-architecture`.
