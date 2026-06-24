#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Супервизор бота — держит bot_control.py всегда запущенным.

  py launcher.py start    запустить под присмотром (перезапуск при падении)
  py launcher.py stop     остановить
  py launcher.py status   показать статус

Бот перезапускается автоматически, если упадёт. Чтобы стартовал при входе в Windows —
положите ярлык launcher.bat в автозапуск (Win+R → shell:startup).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BOT = BASE_DIR / "bot_control.py"
PIDFILE = BASE_DIR / "launcher.pid"


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, errors="replace",
    ).stdout
    return str(pid) in out


def start() -> int:
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text())
            if _alive(old):
                print(f"Уже запущено (pid {old}). Сначала: py launcher.py stop")
                return 1
        except Exception:
            pass

    PIDFILE.write_text(str(os.getpid()))
    print("Супервизор запущен. Держу бота онлайн (перезапуск при падении). Ctrl+C — стоп.")
    proc = None
    try:
        while True:
            proc = subprocess.Popen([sys.executable, str(BOT)])
            code = proc.wait()
            print(f"[launcher] бот завершился (код {code}). Перезапуск через 5с…")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[launcher] остановка по Ctrl+C")
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
    finally:
        try:
            PIDFILE.unlink()
        except Exception:
            pass
    return 0


def stop() -> int:
    if not PIDFILE.exists():
        print("Не запущено (нет launcher.pid).")
        return 1
    try:
        pid = int(PIDFILE.read_text())
    except Exception:
        PIDFILE.unlink(missing_ok=True)
        print("Повреждён launcher.pid — удалён.")
        return 1
    # /T — завершить и дочерний процесс бота, /F — принудительно
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    PIDFILE.unlink(missing_ok=True)
    print(f"Остановлено (pid {pid}).")
    return 0


def status() -> int:
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text())
            if _alive(pid):
                print(f"Запущено (pid {pid}).")
                return 0
        except Exception:
            pass
    print("Не запущено.")
    return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = (sys.argv[1].lower() if len(sys.argv) > 1 else "start")
    fn = {"start": start, "stop": stop, "status": status}.get(cmd)
    if not fn:
        print("Использование: py launcher.py [start|stop|status]")
        return 2
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
