#!/usr/bin/env bash
# Send one WAV clip (16kHz mono 16-bit) to /voice.
# Usage: server/try_voice.sh clip.wav [base-url]
set -euo pipefail

wav="${1:?usage: server/try_voice.sh <clip.wav> [base-url]}"
base="${2:-http://localhost:8000}"

curl --fail-with-body -s -X POST "$base/voice" \
  -F "audio=@$wav;type=audio/wav"
echo
