import json
import os
import threading
import time
from pathlib import Path
from datetime import datetime, timezone


RUN_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "run_state.json"
RUN_STATE_LASTGOOD_PATH = RUN_STATE_PATH.with_suffix(RUN_STATE_PATH.suffix + ".lastgood")
_RUN_STATE_LOCK = threading.Lock()


def _backup_corrupted_run_state():
    if not RUN_STATE_PATH.exists():
        return
    stamp = int(time.time())
    backup = RUN_STATE_PATH.with_suffix(RUN_STATE_PATH.suffix + f".corrupt-{stamp}.bak")
    try:
        backup.write_bytes(RUN_STATE_PATH.read_bytes())
    except Exception:
        pass


def _read_json_file(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_run_state():
    if not RUN_STATE_PATH.exists():
        return {}
    try:
        return _read_json_file(RUN_STATE_PATH)
    except json.JSONDecodeError:
        _backup_corrupted_run_state()
        if RUN_STATE_LASTGOOD_PATH.exists():
            try:
                return _read_json_file(RUN_STATE_LASTGOOD_PATH)
            except Exception:
                return {}
        return {}
    except Exception:
        return {}


def save_run_state(state: dict):
    payload = dict(state or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_main = RUN_STATE_PATH.with_suffix(RUN_STATE_PATH.suffix + ".tmp")
    tmp_last = RUN_STATE_LASTGOOD_PATH.with_suffix(RUN_STATE_LASTGOOD_PATH.suffix + ".tmp")
    with _RUN_STATE_LOCK:
        with tmp_main.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_main, RUN_STATE_PATH)

        with tmp_last.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_last, RUN_STATE_LASTGOOD_PATH)


def clear_run_state():
    with _RUN_STATE_LOCK:
        if RUN_STATE_PATH.exists():
            RUN_STATE_PATH.unlink()
        if RUN_STATE_LASTGOOD_PATH.exists():
            RUN_STATE_LASTGOOD_PATH.unlink()
