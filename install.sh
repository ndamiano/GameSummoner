#!/usr/bin/env bash
# One-time setup for `GameSummoner` on this machine.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
home=${GAMESUMMONER_HOME:-$HOME/.gamesummoner}
images=${IMAGES_REPO_DIR:-$home/images}
workers=${WORKER_REPO_DIR:-$home/workers}
swap=${LLAMA_SWAP_URL:-http://localhost:8080}
settings="$root/src/config/settings.json"
missing=0

need() { echo "missing: $1" >&2; missing=1; }
command -v python3.12 >/dev/null 2>&1 || need "Python 3.12"
command -v node >/dev/null 2>&1 || need "Node 20"
command -v git >/dev/null 2>&1 || need "git"
[ "$missing" = 0 ] || { echo "install stopped: everything above is required" >&2; exit 1; }

plan=()
[ -d "$images" ] || plan+=("gamesummoner-images, cloned to $images")
[ -d "$workers" ] || plan+=("gamesummoner-workers, cloned to $workers")
plan+=("Python packages in $root/venv" "Playwright's Chromium, for play-testing games"
       "Node packages in $root/runtime/node_modules" "GPU worker packages in $home/venv-gpu"
       "your model choices in $settings" "the GameSummoner command in $HOME/.local/bin")
rc=0
picked=$(python3.12 "$root/scripts/install_models.py" "$swap" "${plan[@]}") || rc=$?
case $rc in
    0) ;;
    3 | 130) echo "nothing installed" >&2; exit 1 ;;
    *) echo "install stopped: everything above is required (docs/llama-swap.md)" >&2; exit 1 ;;
esac
{ read -r llm; read -r decide; read -r comfy; } <<<"$picked"

[ -d "$images" ] || git clone -q https://github.com/ndamiano/gamesummoner-images "$images"
[ -d "$workers" ] || git clone -q https://github.com/ndamiano/gamesummoner-workers "$workers"

[ -d "$root/venv" ] || python3.12 -m venv "$root/venv"
"$root/venv/bin/pip" install -q -r "$root/requirements.txt"
"$root/venv/bin/python" -m playwright install chromium
(cd "$root/runtime" && npm ci --silent)
[ -d "$home/venv-gpu" ] || python3.12 -m venv "$home/venv-gpu"
"$home/venv-gpu/bin/pip" install -q -r "$images/local/requirements.txt"

fresh=0
[ -f "$settings" ] || { cp "$root/src/config/settings.example.json" "$settings"; fresh=1; }
"$root/venv/bin/python" - "$settings" "$swap" "$llm" "$decide" "$comfy" "$fresh" "$home/runs" <<'PY'
import json, sys
path, url, llm, decide, comfy, fresh, runs = sys.argv[1:]
d = json.load(open(path))
d.setdefault("llm", {})["model"] = llm
if fresh == "1":
    d["working_directory"] = runs
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

echo "installed: run GameSummoner"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH first" ;; esac
