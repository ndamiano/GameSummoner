#!/usr/bin/env bash
# One-time setup for `GameSummoner summon "<request>"` on this machine.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
missing=0

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 — $2" >&2; missing=1; }; }
need_dir() { [ -e "$1" ] || { echo "missing: $1 — $2" >&2; missing=1; }; }

need_cmd python3.12 "Python 3.12"
need_cmd node "Node 20"
need_cmd npm "Node 20"
need_cmd nvidia-smi "an NVIDIA driver"
need_cmd docker "Docker, which runs the decide box"
need_dir "${BASALT_DIR:-$HOME/Documents/basalt}/bin/basalt" "Basalt, which serves the llm (set BASALT_DIR)"
need_dir "${BASALT_CONFIG:-/var/lib/models/basalt/basalt-iq3s.json}" "Basalt's model config (set BASALT_CONFIG)"
need_dir "${COMFY_DIR:-$HOME/comfy/mess-with-comfy}/main.py" "ComfyUI v0.39.2 (set COMFY_DIR)"
need_dir "${COMFY_PYTHON:-$HOME/Documents/Comfy/comfy-env/bin/python}" "ComfyUI's python (set COMFY_PYTHON)"
need_dir "${IMAGES_REPO_DIR:-$HOME/Documents/gamesummoner-images}/contract" "gamesummoner-images (set IMAGES_REPO_DIR)"
need_dir "${WORKER_REPO_DIR:-$HOME/Documents/gamesummoner-workers}/src" "gamesummoner-workers (set WORKER_REPO_DIR)"
[ "$missing" = 0 ] || { echo "install stopped: everything above is required" >&2; exit 1; }

[ -d "$root/venv" ] || python3.12 -m venv "$root/venv"
"$root/venv/bin/pip" install -q -r "$root/requirements.txt"
"$root/venv/bin/python" -m playwright install chromium
(cd "$root/runtime" && npm ci --silent)
[ -f "$root/src/config/settings.json" ] || cp "$root/src/config/settings.example.json" "$root/src/config/settings.json"

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/GameSummoner" <<EOF
#!/usr/bin/env bash
exec "$root/venv/bin/python" "$root/scripts/summon.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/GameSummoner"

echo "installed: GameSummoner summon \"A game about frogs eating bugs\""
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH first" ;; esac
