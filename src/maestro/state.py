import json
import os
import uuid
from pathlib import Path
from typing import Dict, Optional

from tools.execution_context import resolve_base_path

SPEC_FILE = "spec.json"


class RunState:
    def __init__(self, run_id):
        self.run_id = run_id
        self.run_dir = resolve_base_path() / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def spec_path(self) -> Path:
        return self.run_dir / SPEC_FILE

    def write_spec(self, spec: Dict) -> None:
        self._write(SPEC_FILE, spec)

    def read_spec(self) -> Optional[Dict]:
        return self._read(SPEC_FILE)

    def _write(self, filename: str, data) -> None:
        # Atomic: a concurrent reader must never see a half-written file. Write a unique temp in the same dir, then os.replace (atomic on POSIX) — a reader
        # sees either the old complete file or the new one, never a truncated one.
        path = self.run_dir / filename
        tmp = self.run_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _read(self, filename: str):
        path = self.run_dir / filename
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
