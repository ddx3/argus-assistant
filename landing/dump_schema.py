"""Snapshot of the settings schema of the real panel (with an empty config) in out/_schema.json.
Run it with the project's Python (`.venv`): it needs pydantic and the rest."""
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from argus.core.config import Config  # noqa: E402
from argus.ui import admin  # noqa: E402

tmp = Path(tempfile.mkdtemp())
cfg = Config()
cfg.root = tmp
cfg.config_path = tmp / "argus.toml"
(tmp / "argus.toml").write_text("", encoding="utf-8")
app = types.SimpleNamespace(cfg=cfg, assistant=None, chats=None)
out = Path(__file__).parent / "out" / "_schema.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(admin.build_panel(app).schema(), ensure_ascii=False), encoding="utf-8")
print("written", out)
