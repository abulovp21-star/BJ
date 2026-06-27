#!/usr/bin/env python3
"""
run.py — запускает webapp_server.py и bj.py вместе на Railway
"""
import subprocess, os, sys, time, signal, threading

HERE = os.path.dirname(os.path.abspath(__file__))
PY   = sys.executable
procs = []

def pipe_output(proc, tag):
    def _reader():
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    print(f"[{tag}] {line}", flush=True)
        except Exception:
            pass
    threading.Thread(target=_reader, daemon=True).start()

def kill_all(sig=None, frame=None):
    for p in reversed(procs):
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass
    sys.exit(0)

signal.signal(signal.SIGINT,  kill_all)
signal.signal(signal.SIGTERM, kill_all)

def start(fname, tag):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen(
        [PY, os.path.join(HERE, fname)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    pipe_output(p, tag)
    procs.append(p)
    return p

def main():
    print("▶ Запуск webapp_server.py…", flush=True)
    srv = start("webapp_server.py", "server")
    time.sleep(2)

    print("▶ Запуск bj.py…", flush=True)
    bot = start("bj.py", "bot")

    print("✅ Оба процесса запущены", flush=True)

    files = [("webapp_server.py", "server"), ("bj.py", "bot")]
    running = [srv, bot]

    while True:
        for i, (p, (fname, tag)) in enumerate(zip(running, files)):
            code = p.poll()
            if code is not None:
                print(f"✗ {fname} упал (код {code}). Перезапуск через 5 сек…", flush=True)
                time.sleep(5)
                new = start(fname, tag)
                running[i] = new
        time.sleep(5)

if __name__ == "__main__":
    main()
