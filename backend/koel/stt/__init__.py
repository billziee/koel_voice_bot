"""STT_svc — speech-to-text (PRD §05/§07: Whisper on SageMaker).

The orchestrator depends only on the provider interface, so the SageMaker
Whisper endpoint swaps in behind the same contract (PRD §12 vendor-lock-in
mitigation). A stub provider ships for local, GPU-free demo.
"""
