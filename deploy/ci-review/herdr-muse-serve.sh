#!/bin/bash
# Serve Muse-Glimmer-30B for the always-on review's second tier-1 reviewer: llama.cpp CUDA server on loopback
# 127.0.0.1:28931 (OpenAI-compatible, alias muse-glimmer-30b), one slot (-np 1), context 98304. Modeled on
# ~/bench-lfm/serve-next.sh (muse case). Needs the GPU free. Run by herdr-muse-serve.service, or by hand:
#   deploy/ci-review/herdr-muse-serve.sh [MODEL_DIR]      (default ~/bench-lfm/models)
# Not installed or started by anything in the repo. Stop with: docker rm -f herdr-muse-srv
set -euo pipefail
MODELS=${1:-$HOME/bench-lfm/models}
NAME=herdr-muse-srv
IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda-b11804
MODEL=Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf
[ -f "$MODELS/$MODEL" ] || { echo "missing $MODELS/$MODEL" >&2; exit 1; }
docker rm -f "$NAME" >/dev/null 2>&1 || true
# Foreground (no -d) so systemd supervises the container; --rm cleans it up. Published on loopback only.
exec docker run --rm --name "$NAME" --gpus all -p 127.0.0.1:28931:8080 -v "$MODELS":/models:ro \
  "$IMAGE" -m "/models/$MODEL" --alias muse-glimmer-30b \
  --host 0.0.0.0 --port 8080 --jinja -c 98304 -np 1 -ngl 99
