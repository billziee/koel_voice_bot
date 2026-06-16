"""NLP_svc — intent + response generation (PRD §05/§07: AWS Bedrock + RAG).

Emits structured output on every turn: response text, intent tag, escalation
flag, and a tone tag (PRD §06 Tone & Prosody Control; §14 100% tone coverage).
RAG retrieval is grounded in the pgvector knowledge base so product facts are
never free-form generated (PRD §12 hallucination mitigation).
"""
