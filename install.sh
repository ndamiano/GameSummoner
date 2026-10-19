#!/usr/bin/env bash
# One-time setup for `GameSummoner summon "<request>"` on this machine.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
home=${GAMESUMMONER_HOME:-$HOME/.gamesummoner}
images=${IMAGES_REPO_DIR:-$home/images}
workers=${WORKER_REPO_DIR:-$home/workers}
swap=${LLAMA_SWAP_URL:-http://localhost:8080}
llm=${GS_LLM_MODEL:-gamesummoner-llm}
decide=${GS_DECIDE_MODEL:-gamesummoner-decide}
comfy=${GS_COMFY_MODEL:-gamesummoner-comfy}
missing=0

need() { echo "missing: $1" >&2; missing=1; }
command -v python3.12 >/dev/null 2>&1 || need "Python 3.12"
command -v node >/dev/null 2>&1 || need "Node 20"
command -v git >/dev/null 2>&1 || need "git"

models=$(curl -fsS -m 5 "$swap/v1/models" 2>/dev/null || true)
[ -n "$models" ] || need "llama-swap at $swap (set LLAMA_SWAP_URL)"
if [ -n "$models" ]; then
    for m in "$llm" "$decide"; do
        grep -q "\"$m\"" <<<"$models" || need "model '$m' in llama-swap (set GS_LLM_MODEL / GS_DECIDE_MODEL)"
    done
    echo "checking ComfyUI through llama-swap ('$comfy'); the first start can take a minute"
    curl -fsS -m 300 -o /dev/null "$swap/upstream/$comfy/system_stats" \
        || need "ComfyUI as model '$comfy' in llama-swap (set GS_COMFY_MODEL)"
fi
[ "$missing" = 0 ] || { echo "install stopped: everything above is required (docs/llama-swap.md)" >&2; exit 1; }

[ -d "$images" ] || git clone -q https://github.com/ndamiano/gamesummoner-images "$images"
[ -d "$workers" ] || git clone -q https://github.com/ndamiano/gamesummoner-workers "$workers"

[ -d "$root/venv" ] || python3.12 -m venv "$root/venv"
"$root/venv/bin/pip" install -q -r "$root/requirements.txt"
"$root/venv/bin/python" -m playwright install chromium
(cd "$root/runtime" && npm ci --silent)
[ -d "$home/venv-gpu" ] || python3.12 -m venv "$home/venv-gpu"
"$home/venv-gpu/bin/pip" install -q -r "$images/local/requirements.txt"

settings="$root/src/config/settings.json"
[ -f "$settings" ] || cp "$root/src/config/settings.example.json" "$settings"
"$root/venv/bin/python" - "$settings" "$swap" "$llm" "$decide" "$comfy" <<'PY'
import json, sys
path, url, llm, decide, comfy = sys.argv[1:]
d = json.load(open(path))
d.setdefault("llm", {})["model"] = llm
d.setdefault("local", {})["llama_swap"] = {"url": url, "decide": decide, "comfy": comfy}
json.dump(d, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/GameSummoner" <<EOF
#!/usr/bin/env bash
export IMAGES_REPO_DIR="$images" WORKER_REPO_DIR="$workers"
exec "$root/venv/bin/python" "$root/scripts/summon.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/GameSummoner"

echo "installed: GameSummoner summon \"A game about frogs eating bugs\""
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH first" ;; esac
