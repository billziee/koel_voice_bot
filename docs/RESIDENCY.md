# Data Residency — accepted risk (as of this build)

## Decision
For the current phase KOEL runs its AI inference on **hosted US providers**:

| Capability | Provider | Endpoint | Region |
|---|---|---|---|
| NLP (intent + response) | Groq | `api.groq.com` | US |
| STT (Whisper) | Groq | `api.groq.com` | US |
| TTS (English — Orpheus) | Groq | `api.groq.com` | US |
| TTS (Hindi/Hinglish — Magpie) | NVIDIA NVCF | `grpc.nvcf.nvidia.com` | US |
| Embeddings (RAG) | NVIDIA NIM / AWS Bedrock | see `KOEL_NLP_EMBEDDER` | US / ap-south-1 |

## The gap
PRD §11 mandates **India data residency (AWS ap-south-1)** under the DPDP Act 2023
("All compute and storage in AWS ap-south-1; Bedrock VPC-scoped so data does not
leave the boundary"). The hosted Groq/NVIDIA path **does not meet this** — customer
audio and text (and any PII in the prompt) egress to the United States. Neither Groq
nor NVIDIA NVCF currently offers an India region or a no-retention/region-pinning DPA.

**This is an explicitly accepted, signed-off risk for this phase**, not an oversight.

## Mitigations in place
- **PII minimisation before egress**: structured PII (name/phone/account) is masked
  before text is sent to Groq where it does not break intent (see `nlp` input hardening).
- **DPDP field minimisation**: only use-case-relevant CRM fields load into a session.
- **Transport encryption**: TLS to all providers; DB `sslmode=require`, Redis `rediss://` in prod.
- **Embeddings can stay in-region**: set `KOEL_NLP_EMBEDDER=bedrock` to use AWS Bedrock
  Titan v2 (ap-south-1) for KB embeddings instead of NVIDIA NIM (US).

## The compliant path (when required)
To meet PRD §11, self-host the models in ap-south-1:
- **NLP** → AWS Bedrock (Claude/Titan) in ap-south-1, or a self-hosted LLM NIM.
- **STT** → self-hosted Whisper / Riva ASR NIM on ap-south-1 GPU (EC2/ECS/EKS).
- **TTS** → self-hosted Magpie / Orpheus NIM on ap-south-1 GPU.
- **Embeddings** → Bedrock Titan v2 (already an option today).

The provider abstraction (`*/providers.py` + config-selected provider) makes this a
config + new-provider-class change, not an orchestrator rewrite. Expect ~30–60% higher
speech-inference cost for the in-region GPU footprint. Track under Plan workstream
"Data residency & secrets".

## Secrets
API keys live only in the gitignored `backend/.env` (never committed). In production
they must come from a secret manager (AWS Secrets Manager / SSM), not `.env`. Keys that
have been pasted into chat/logs during development **must be rotated** before any real use.
