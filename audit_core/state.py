import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


POST_AUDIT_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "post_audit_state.json"
POST_AUDIT_LASTGOOD_PATH = POST_AUDIT_STATE_PATH.with_suffix(POST_AUDIT_STATE_PATH.suffix + ".lastgood")
_STATE_LOCK = threading.Lock()


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _backup_corrupted_state():
    if not POST_AUDIT_STATE_PATH.exists():
        return
    stamp = int(time.time())
    backup = POST_AUDIT_STATE_PATH.with_suffix(POST_AUDIT_STATE_PATH.suffix + f".corrupt-{stamp}.bak")
    try:
        backup.write_bytes(POST_AUDIT_STATE_PATH.read_bytes())
    except Exception:
        pass


def load_post_audit_state():
    if not POST_AUDIT_STATE_PATH.exists():
        return {}
    try:
        return _read_json(POST_AUDIT_STATE_PATH)
    except json.JSONDecodeError:
        _backup_corrupted_state()
        if POST_AUDIT_LASTGOOD_PATH.exists():
            try:
                return _read_json(POST_AUDIT_LASTGOOD_PATH)
            except Exception:
                return {}
        return {}
    except Exception:
        return {}


def save_post_audit_state(state: dict):
    payload = dict(state or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    POST_AUDIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_main = POST_AUDIT_STATE_PATH.with_suffix(POST_AUDIT_STATE_PATH.suffix + ".tmp")
    tmp_last = POST_AUDIT_LASTGOOD_PATH.with_suffix(POST_AUDIT_LASTGOOD_PATH.suffix + ".tmp")
    with _STATE_LOCK:
        with tmp_main.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_main, POST_AUDIT_STATE_PATH)

        with tmp_last.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_last, POST_AUDIT_LASTGOOD_PATH)


def clear_post_audit_state():
    with _STATE_LOCK:
        if POST_AUDIT_STATE_PATH.exists():
            POST_AUDIT_STATE_PATH.unlink()
        if POST_AUDIT_LASTGOOD_PATH.exists():
            POST_AUDIT_LASTGOOD_PATH.unlink()
