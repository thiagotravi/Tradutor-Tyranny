import atexit
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


LOCK_PATH = Path(__file__).resolve().parents[1] / "data" / "app_instance.lock"


class InstanceAlreadyRunningError(RuntimeError):
    def __init__(self, pid: int, lock_path: Path):
        super().__init__(f"Instance already running with pid={pid}")
        self.pid = pid
        self.lock_path = lock_path


def _pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class AppInstanceLock:
    lock_path: Path
    pid: int
    acquired: bool = False

    def release(self):
        if not self.acquired:
            return
        try:
            if self.lock_path.exists():
                current_pid = None
                try:
                    payload = json.loads(self.lock_path.read_text(encoding="utf-8") or "{}")
                    current_pid = int(payload.get("pid", 0) or 0)
                except Exception:
                    current_pid = None
                # Remove apenas se este processo for o dono (ou metadata corrompida).
                if current_pid in (None, self.pid):
                    self.lock_path.unlink(missing_ok=True)
        finally:
            self.acquired = False


def ensure_single_instance() -> AppInstanceLock:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    # Se lock já existe, valida se é stale.
    if LOCK_PATH.exists():
        stale = False
        existing_pid = 0
        try:
            payload = json.loads(LOCK_PATH.read_text(encoding="utf-8") or "{}")
            existing_pid = int(payload.get("pid", 0) or 0)
            if existing_pid == current_pid:
                lock = AppInstanceLock(lock_path=LOCK_PATH, pid=current_pid, acquired=True)
                atexit.register(lock.release)
                return lock
            stale = not _pid_is_alive(existing_pid)
        except Exception:
            stale = True
        if stale:
            LOCK_PATH.unlink(missing_ok=True)
        else:
            raise InstanceAlreadyRunningError(existing_pid, LOCK_PATH)

    # Criação atômica da trava.
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        payload = {
            "pid": current_pid,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        os.close(fd)

    lock = AppInstanceLock(lock_path=LOCK_PATH, pid=current_pid, acquired=True)
    atexit.register(lock.release)
    return lock
