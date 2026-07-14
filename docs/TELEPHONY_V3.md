# KOEL — Telephony v3 (streaming ingest seam built)

> Supersedes [TELEPHONY.md](./TELEPHONY.md). v2 had the µ-law seam + a documented
> PSTN bridge; v3 built the streaming-session ingest abstraction the bridge rides on.

## Evolution (v1 → v2 → v3)

| Aspect | v1 | v2 | v3 |
|---|---|---|---|
| Transport | browser WS only | browser WS only | + **`telephony/ingest.py` `StreamingSessionFactory`/`StreamingSttSession`** abstraction |
| Codec | 16k PCM | + µ-law seam | µ-law seam + Deepgram `linear16` streaming |
| STT ingest | batch per-utterance | batch | **continuous-frame streaming session** (shared by browser Deepgram + future PSTN) |
| PSTN bridge | — | documented (Twilio Media Streams) | **seam ready** — the bridge is a thin adapter onto the same factory |

## v3 state
The browser streaming STT path (O-6 Wave 2, `ws/streaming_stt.py`) and any future
Twilio/Exotel bridge share **one** ingest seam (`StreamingSessionFactory`): open a
long-lived session, stream frames in, get partials + finals out. So Theme-1
(1.1 Twilio / 1.4 Exotel) is now an adapter-onto-a-proven-seam job, not a
from-scratch build. DTMF (1.3) and the outbound dialer (1.2) remain to build.
