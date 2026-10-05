"""Long-running project services (dashboard, phone server): start if down, restart after a deploy."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOM_PY = str(Path(__file__).resolve().parent.parent / "room.py")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def declared(cfg: dict) -> dict[str, dict]:
    services = {"dashboard": {"command": [sys.executable, ROOM_PY, "dashboard"], "port": 8770}}
    for name, spec in (cfg.get("services") or {}).items():
        services[name] = {"command": spec["command"], "port": spec.get("port"),
                          "restart_on_deploy": spec.get("restart_on_deploy", False)}
    return services


def start(cfg: dict, name: str, spec: dict, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"service-{name}.log", "a", encoding="utf-8")
    # CREATE_NO_WINDOW gives the service a hidden console that its children inherit. DETACHED_PROCESS would
    # make a shell's child allocate a fresh, visible console (Windows Terminal pops up).
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    command = spec["command"]
    subprocess.Popen(command, cwd=cfg["_root"], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     shell=isinstance(command, str), creationflags=flags, close_fds=True)


def ensure(cfg: dict, log_dir: Path) -> list[str]:
    lines = []
    for name, spec in declared(cfg).items():
        if spec.get("port") and port_open(int(spec["port"])):
            lines.append(f"{name}: already running on :{spec['port']}")
            continue
        start(cfg, name, spec, log_dir)
        lines.append(f"{name}: started" + (f" on :{spec['port']}" if spec.get("port") else ""))
    return lines


def stop_port(port: int) -> None:
    """Stop whatever listens on a local port (Windows: netstat + taskkill)."""
    if os.name != "nt":
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        return
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = {line.split()[-1] for line in out.splitlines()
            if "LISTENING" in line and line.split()[1].endswith(f":{port}")}
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)


def restart_after_deploy(cfg: dict, log_dir: Path) -> list[str]:
    """Services that run project code are restarted so they serve what was just merged."""
    lines = []
    for name, spec in declared(cfg).items():
        if not spec.get("restart_on_deploy") or not spec.get("port"):
            continue
        stop_port(int(spec["port"]))
        for _ in range(20):
            if not port_open(int(spec["port"])):
                break
            time.sleep(0.25)
        start(cfg, name, spec, log_dir)
        lines.append(f"restarted {name} on :{spec['port']}")
    return lines
