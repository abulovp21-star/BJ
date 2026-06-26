#!/usr/bin/env python3
"""
run.py — запуск всего из одного терминала
"""
import subprocess, os, sys, time, re, signal, threading

BOT_TOKEN = os.getenv("BOT_TOKEN", "8161712628:AAHdnTBNyNehzvK4S0kMqnZh2spMtl5NEfU")
DB_DSN    = os.getenv("DB_DSN",    "postgresql://localhost/bjbot")
PORT      = 8080

HERE  = os.path.dirname(os.path.abspath(__file__))
PY    = sys.executable
procs = []

COLORS = {
    "cf":     "\033[36m",
    "server": "\033[33m",
    "bot":    "\033[32m",
    "reset":  "\033[0m",
}

def log(tag, line):
    c = COLORS.get(tag, "")
    print(f"{c}[{tag:6s}]{COLORS['reset']} {line}", flush=True)

def pipe_output(proc, tag):
    """Читаем stdout и stderr в одном потоке через stderr=STDOUT."""
    def _reader():
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    log(tag, line)
        except Exception:
            pass
    t = threading.Thread(target=_reader, daemon=True)
    t.start()

def kill_all(sig=None, frame=None):
    print("\n\033[31mОстановка всех процессов…\033[0m")
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

from pyngrok import ngrok as pyngrok

def start_cloudflared(port, max_wait=30):
    url = pyngrok.connect(port).public_url.replace("http://", "https://")
    print(f"ngrok URL: {url}")
    class FakeProc:
        def terminate(self): pyngrok.kill()
        def wait(self, timeout=0): pass
    return FakeProc(), url

def _unused(port, max_wait=30):
    p = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}", "--protocol", "http2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # stderr → stdout
        text=True, bufsize=1,
    )
    procs.append(p)
    found = {"url": None}

    def _reader():
        for raw in p.stdout:
            line = raw.rstrip()
            if line:
                log("cf", line)
            m = CF_URL_RE.search(line)
            if m and not found["url"]:
                found["url"] = m.group(0)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    deadline = time.time() + max_wait
    while time.time() < deadline:
        if found["url"]:
            return p, found["url"]
        time.sleep(0.5)
    return p, None

def start(args, env, tag):
    p = subprocess.Popen(
        args, env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # ← фикс: stderr тоже в stdout
        text=True, bufsize=1,
    )
    pipe_output(p, tag)
    procs.append(p)
    return p

def main():
    env = os.environ.copy()
    env["BOT_TOKEN"]        = BOT_TOKEN
    env["DB_DSN"]           = DB_DSN
    env["PYTHONUNBUFFERED"] = "1"

    print("\033[36m▶ Запуск cloudflared…\033[0m")
    print("⏳ Ожидание URL (до 30 сек)…")
    cf, url = start_cloudflared(PORT, 30)

    if not url:
        print("\033[31m✗ cloudflared не ответил.\033[0m")
        print("  Убедись что установлен:  which cloudflared")
        print("  Если нет:  pkg install -y cloudflared")
        kill_all()

    print(f"\033[36m✓ cloudflared: {url}\033[0m")
    env["WEBAPP_URL"] = url

    print("\033[33m▶ Запуск webapp_server.py…\033[0m")
    srv = start([PY, os.path.join(HERE, "webapp_server.py")], env, "server")
    time.sleep(2)

    print("\033[32m▶ Запуск bj.py…\033[0m")
    bot = start([PY, os.path.join(HERE, "bj.py")], env, "bot")

    print(f"""
\033[1m✅ Всё запущено\033[0m
   Мини-апп : \033[36m{url}\033[0m
   Ctrl+C   : остановить всё
""")


    while True:
        for p, name, fname in [(srv, "server", "webapp_server"), (bot, "bot", "bj")]:
            code = p.poll()
            if code is not None:
                print(f"[31m✗ {fname}.py упал (код {code}). Перезапуск через 5 сек…[0m")
                time.sleep(5)
                new = start([PY, os.path.join(HERE, f"{fname}.py"), ], env, name)
                if p in procs:
                    procs[procs.index(p)] = new
        time.sleep(5)

if __name__ == "__main__":
    main()
