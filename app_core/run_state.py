import json
from pathlib import Path
from datetime import datetime, timezone


RUN_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "run_state.json"


def load_run_state():
    if not RUN_STATE_PATH.exists():
        return {}
    try:
        with RUN_STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_run_state(state: dict):
    payload = dict(state or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def clear_run_state():
    if RUN_STATE_PATH.exists():
        RUN_STATE_PATH.unlink()
