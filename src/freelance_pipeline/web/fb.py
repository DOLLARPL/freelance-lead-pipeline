"""Facebook: rozpoznanie autora posta i wysylka (priv + komentarz) przez worker M1.

Dwa kroki, zeby Jan widzial, co wyjdzie, zanim cokolwiek pojdzie do klienta:
  plan(url)  - czyta post i mowi, czy da sie napisac priv
  send(url)  - publikuje (wolane tylko po kliknieciu "Wyślij")
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid

SCRIPT = "/opt/data/scripts/snajper-fb"
DEFAULT_DM = "Dzień dobry, piszę w sprawie ogłoszenia. Chętnie pomogę."
FOLLOWUP = "Napisałem na priv."
DEFAULT_COMMENT = "Napisz do nas na priv lub przez jnxautomation.pl, chętnie pomożemy."

_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _parse(stdout: str, stderr: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def _worker(task_id: str, cmd: list[str]) -> None:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        payload = _parse(res.stdout, res.stderr)
        with _LOCK:
            _TASKS[task_id] = {
                "state": "done" if payload and payload.get("ok") else "error",
                "result": payload,
                "stderr": "" if payload else ((res.stderr or res.stdout or "")[-400:]),
                "at": time.time(),
            }
    except subprocess.TimeoutExpired:
        with _LOCK:
            _TASKS[task_id] = {"state": "error", "error": "przekroczony czas (5 min)", "at": time.time()}
    except OSError as exc:
        with _LOCK:
            _TASKS[task_id] = {"state": "error", "error": str(exc), "at": time.time()}


def start(command: str, url: str, dm: str = "", comment: str = "") -> str:
    task_id = uuid.uuid4().hex[:10]
    with _LOCK:
        _TASKS[task_id] = {"state": "running", "at": time.time()}
    cmd = [SCRIPT, command, url]
    if dm:
        cmd += ["--dm", dm]
    if comment:
        cmd += ["--comment", comment]
    if command == "send":
        cmd.append("--live")
    threading.Thread(target=_worker, args=(task_id, cmd), daemon=True).start()
    return task_id


def status(task_id: str) -> dict:
    with _LOCK:
        return _TASKS.get(task_id) or {"state": "brak"}
