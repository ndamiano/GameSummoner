import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "scripts"))

from install_models import MODEL_FILE, missing


def _object_info() -> dict:
    info: dict = {}
    for path in (ROOT / "src" / "config" / "workflows").glob("*.json"):
        for node in json.loads(path.read_text()).values():
            if not isinstance(node, dict) or "class_type" not in node:
                continue
            required = info.setdefault(node["class_type"], {"input": {"required": {}}})["input"]["required"]
            for name, value in node["inputs"].items():
                if isinstance(value, str) and MODEL_FILE.search(value):
                    required.setdefault(name, [[]])[0].append(value)
    return info


def test_comfy_check_names_each_missing_node_and_model_file_across_both_option_formats():
    info = _object_info()
    assert missing(info) == []

    del info["BiRefNetRMBG"]
    info["UNETLoader"]["input"]["required"]["unet_name"] = [["other.safetensors"]]
    info["VAELoader"]["input"]["required"]["vae_name"] = [
        "COMBO", {"options": info["VAELoader"]["input"]["required"]["vae_name"][0]}]

    gaps = missing(info)
    assert "node BiRefNetRMBG" in gaps
    assert "model qwen_image_2512_fp8_e4m3fn.safetensors (UNETLoader)" in gaps
    assert "model trellis_2_bf16.safetensors (UNETLoader)" in gaps
    assert not any("VAELoader" in g for g in gaps)
