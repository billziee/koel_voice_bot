#!/usr/bin/env bash
# Fetch the smart-turn-v3.2 ONNX model (8.3MB, BSD-2, from pipecat-ai/pipecat)
# used by the semantic end-of-turn gate (KOEL_SMART_TURN_ENABLED). The model is
# not committed to keep the repo lean; run this at deploy/dev setup. Requires
# the `smart-turn` extra: pip install -e ".[smart-turn]"
set -euo pipefail
DEST="${1:-$(dirname "$0")/../models/smart-turn-v3.2-cpu.onnx}"
URL="https://raw.githubusercontent.com/pipecat-ai/pipecat/main/src/pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx"
mkdir -p "$(dirname "$DEST")"
echo "Downloading smart-turn-v3.2 → $DEST"
curl -fsSL "$URL" -o "$DEST"
echo "Done: $(du -h "$DEST" | cut -f1)"
