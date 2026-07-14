# KOEL — Security Posture v3

> Supersedes [SECURITY.md](./SECURITY.md) (v1/v2 controls). v3 adds the controls
> that came with multi-worker scale, multi-tenancy, and BYOK. Complements
> [RESIDENCY_V3.md](./RESIDENCY_V3.md).

## Control evolution (v1 → v2 → v3)

| Control | v1 | v2 | v3 |
|---|---|---|---|
| Transport TLS (DB/Redis/ingress) | dev plaintext | documented (`?ssl=require`, `rediss://`, HSTS) | unchanged; Redis TLS now load-bearing (rate limiter) |
| PII redaction (NLP + TTS egress) | — | `redact_pii` both paths | + applied per-sentence on the streaming path |
| PII at rest in caches | n/a | n/a | **TTS-cache TTL bounds derived-audio PII (O-3), configurable** |
| AuthN (JWT REST + 120s WS token) | ✓ | ✓ | ✓ |
| Token TTL + silent refresh | design | `/refresh` built, FE gap | **FE persists + silently refreshes (Q-2)** |
| Rate limiting | in-mem, single-worker | in-mem | **Redis-backed (O-8), per-route namespace, login fail-closed** |
| Tenant isolation | soft (`org_id` seam) | soft | **enforced org scoping at login + per-row (6.4)** |
| Secret storage | env only | env only | **BYOK secrets encrypted at rest** |
| Outbound-request safety | n/a | n/a | **SSRF guard on all BYOK provider URLs** |
| Security headers / CSP | middleware | middleware | unchanged |

## v3 notes
- **Rate limiter fail policy:** authenticated endpoints fail-open on a Redis blip
  (availability); **`/v1/auth/login` fails closed** (brute-force protection).
- **BYOK:** custom endpoints are validated by an SSRF allowlist/deny (no
  link-local/internal targets) before any request; secrets never logged, never
  returned by the connections API.
- **New US egress:** Deepgram streaming STT joins Groq/NVIDIA on the residency
  ledger — see [RESIDENCY_V3.md](./RESIDENCY_V3.md).

Mandatory production checklist from v2 (TLS everywhere, rotate seeded creds,
collector for metrics) remains in force.
