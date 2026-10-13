"""Command line: `tracd` starts the tracker; `tracd check <link>` reads one product."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

DEFAULT_PORT = 8723
log = logging.getLogger("tracd")


def data_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    if os.environ.get("TRACD_HOME"):
        return Path(os.environ["TRACD_HOME"]).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        home, old = Path(os.environ["APPDATA"]) / "Tracd", Path(os.environ["APPDATA"]) / "TrackYourFavs"
    else:
        home, old = Path.home() / ".tracd", Path.home() / ".trackyourfavs"
    _adopt_old_home(old, home)
    return home


def _adopt_old_home(old: Path, home: Path) -> None:
    """Tracd used to be called TrackYourFavs: keep the watches and settings.

    Safe to repeat: if anything is in use (the old app still running) it is left
    where it is and tried again next start, and the database's write-ahead log is
    merged in before the file is renamed so no recent change is lost."""
    try:
        if old.is_dir() and not (home / "tracd.db").exists():
            if not home.exists():
                old.rename(home)
            else:
                for f in old.iterdir():
                    if not (home / f.name).exists():
                        shutil.move(str(f), str(home / f.name))
        db = home / "trackyourfavs.db"
        if db.exists() and not (home / "tracd.db").exists():
            con = sqlite3.connect(db, timeout=1)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                con.close()
            db.rename(home / "tracd.db")
            for side in ("-wal", "-shm"):
                (home / f"trackyourfavs.db{side}").unlink(missing_ok=True)
    except (OSError, sqlite3.Error):
        pass


def setup_logging(home: Path, quiet: bool = False) -> None:
    home.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = RotatingFileHandler(home / "tracd.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
    log.addHandler(fh)
    if not quiet and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def keep_awake() -> None:
    """Stop the computer sleeping while the tracker runs (released on exit)."""
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        elif sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - nice to have
        pass


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return "watches" in json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            # The server binds the same way, so a port left in TIME_WAIT by the
            # last run counts as free (on Windows this option would mean "share").
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------
# tracd run
# --------------------------------------------------------------------------


async def serve(opts: argparse.Namespace) -> int:
    from aiohttp import web

    from .browser import BrowserFetcher
    from .engine import Engine
    from .notify import Alert, Notifier, beep
    from .samples import DemoFetcher
    from .server import create_app
    from .store import Store

    home = data_dir(opts.home)
    store = Store(home / ("demo.db" if opts.demo else "tracd.db"))
    if opts.demo:
        fetcher = DemoFetcher()
    else:
        fetcher = BrowserFetcher(home / "browser-profile", headless=opts.headless, show=opts.show_browser,
                                 browser_path=opts.browser_path or None, log=log.info)
    notifier = Notifier(store.settings, log=log.warning)
    engine = Engine(
        store, fetcher, notifier,
        opener=(lambda url: None) if opts.demo else webbrowser.open,
        beeper=(lambda: None) if opts.demo else beep,
    )
    host = "0.0.0.0" if opts.lan else "127.0.0.1"
    lan_key = None
    if opts.lan:
        lan_key = store.settings().get("lan_key") or secrets.token_urlsafe(12)
        store.update_settings(lan_key=lan_key)
    app = create_app(store, engine, fetcher, demo=opts.demo, lan_key=lan_key)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    await runner.setup()
    site = web.TCPSite(runner, host, opts.port)
    await site.start()

    url = f"http://127.0.0.1:{opts.port}/"
    s = store.settings()
    ws = store.watches(enabled_only=True)
    log.info(f"Tracd {__version__} is running. Dashboard: {url}")
    if opts.lan:
        log.info(f"On your phone (same Wi-Fi): http://{lan_ip()}:{opts.port}/?key={lan_key}")
    if s.get("ntfy_enabled"):
        log.info(f"ntfy topic: {s['ntfy_topic']}  (subscribe to it in the ntfy app)")
    log.info(f"Watching {len(ws)} size(s). Press Ctrl+C to stop.")
    keep_awake()
    if not opts.no_open:
        webbrowser.open(url)
    if ws and notifier.channels() and not opts.demo:
        asyncio.create_task(notifier.send(Alert(
            "Tracd is running", f"Watching {len(ws)} size{'s' if len(ws) != 1 else ''}. You'll hear from it the moment one is back.",
            url=None)))
    if not opts.demo:
        asyncio.create_task(_warm(fetcher))
    try:
        await engine.run()
    finally:
        await engine.stop()
        await notifier.close()
        if hasattr(fetcher, "close"):
            await fetcher.close()
        await runner.cleanup()
        store.close()
    return 0


async def _warm(fetcher) -> None:
    try:
        await fetcher.start()
    except Exception as e:  # noqa: BLE001
        log.warning(str(e))


def cmd_run(opts: argparse.Namespace) -> int:
    setup_logging(data_dir(opts.home), quiet=opts.quiet)
    if not port_free("127.0.0.1", opts.port):
        if already_running(opts.port):
            print(f"Tracd is already running: http://127.0.0.1:{opts.port}/")
            if not opts.no_open:
                webbrowser.open(f"http://127.0.0.1:{opts.port}/")
            return 0
        print(f"Port {opts.port} is busy. Start with --port {opts.port + 1}")
        return 1
    try:
        return asyncio.run(serve(opts))
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


# --------------------------------------------------------------------------
# tracd check <link>
# --------------------------------------------------------------------------


async def check(opts: argparse.Namespace) -> int:
    from .browser import BrowserFetcher
    from .extract import first_url
    from .models import ShopError
    from .shops import is_short_link, shop_for

    home = data_dir(opts.home)
    url = first_url(opts.link)
    if not url:
        print("That is not a link.")
        return 2
    saved: list[tuple[str, str]] = []
    fetcher = BrowserFetcher(home / "browser-profile", headless=opts.headless, show=opts.show_browser,
                             browser_path=opts.browser_path or None, log=lambda m: print("  " + m))
    if opts.save:
        real = fetcher.fetch

        async def keep(u, **kw):
            f = await real(u, **kw)
            saved.append((u, f.text))
            return f

        fetcher.fetch = keep  # type: ignore[method-assign]
    try:
        if is_short_link(url):
            url = await fetcher.final_url(url)
            print(f"Share link opens: {url}")
        shop = shop_for(url)
        print(f"Shop: {shop.name}")
        t0 = asyncio.get_running_loop().time()
        try:
            p = await shop.read(fetcher, url)
        except ShopError as e:
            print(f"Could not read it: {e.detail}  [{e.state.value}]")
            return 1
        ms = int((asyncio.get_running_loop().time() - t0) * 1000)
        print(f"\n{p.title}" + (f"  ({p.brand})" if p.brand else ""))
        if p.price:
            print(f"Price: ₹{int(p.price):,}")
        if p.note:
            print(f"Note: {p.note}")
        print(f"\n  {'Size':<16}{'Stock':<12}{'Left':<6}Id")
        for s in p.sizes:
            mark = "in stock" if s.available else "sold out" if s.available is False else "?"
            here = "  <- your link" if s.id == p.selected_size_id else ""
            print(f"  {s.label:<16}{mark:<12}{(str(s.qty) if s.qty else ''):<6}{s.id}{here}")
        print(f"\nRead in {ms} ms.")
        if opts.size:
            from .extract import norm_label

            want = [s for s in p.sizes if norm_label(s.label) == norm_label(opts.size)]
            if not want:
                print(f"No size called {opts.size!r} here.")
                return 1
            r = shop.reading(await shop.read(fetcher, shop.poll_url(p.url, want[0].id, want[0].url)), want[0].id, want[0].label)
            print(f"{want[0].label}: {r.state.value} ({r.detail})")
        return 0
    finally:
        await fetcher.close()
        if opts.save and saved:
            d = Path(opts.save)
            d.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for i, (u, text) in enumerate(saved):
                host = urlparse(u).netloc.removeprefix("www.") or "page"
                (d / f"{host}-{stamp}-{i:02d}.txt").write_text(u + "\n" + text, encoding="utf-8")
            print(f"Saved {len(saved)} raw response(s) to {d}")


def cmd_check(opts: argparse.Namespace) -> int:
    try:
        return asyncio.run(check(opts))
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------
# tracd test-alert
# --------------------------------------------------------------------------


def cmd_test_alert(opts: argparse.Namespace) -> int:
    from .notify import Alert, Notifier
    from .store import Store

    store = Store(data_dir(opts.home) / "tracd.db")

    async def go() -> int:
        n = Notifier(store.settings)
        try:
            if not n.channels():
                print("No alert channel is on. Open the dashboard and set up Phone alerts.")
                return 1
            s = store.settings()
            if "ntfy" in n.channels():
                print(f"ntfy topic: {s['ntfy_topic']}")
            res = await n.send(Alert("Tracd test", "If you can read this on your phone, alerts work.", urgent=True))
            for k, v in res.items():
                print(f"{k}: {'sent' if v is None else 'FAILED: ' + v}")
            return 0 if any(v is None for v in res.values()) else 1
        finally:
            await n.close()

    return asyncio.run(go())


# --------------------------------------------------------------------------
# tracd autostart on|off
# --------------------------------------------------------------------------


def _run_args(opts: argparse.Namespace | None = None) -> list[str]:
    """The options the signed-in start should use: the same data folder, port and
    browser choices as now."""
    args = ["-m", "tracd", "--no-open", "--quiet"]
    home = getattr(opts, "home", None) or os.environ.get("TRACD_HOME")
    if home:
        args += ["--home", str(Path(home).expanduser().resolve())]
    port = getattr(opts, "port", DEFAULT_PORT)
    if port and port != DEFAULT_PORT:
        args += ["--port", str(port)]
    for flag in ("lan", "show_browser", "headless"):
        if getattr(opts, flag, False):
            args.append("--" + flag.replace("_", "-"))
    if getattr(opts, "browser_path", ""):
        args += ["--browser-path", opts.browser_path]
    return args


def _desktop_quote(a: str) -> str:
    """Quote one argument for a .desktop Exec line (freedesktop rules)."""
    if a and not any(c in a for c in " \t\n\"'\\$`<>|&;()*?#~=%"):
        return a
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in a).replace("%", "%%") + '"'


def _startup_target(opts: argparse.Namespace | None = None) -> tuple[Path, str]:
    from xml.sax.saxutils import escape

    py = Path(sys.executable)
    args = _run_args(opts)
    if sys.platform == "win32":
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else py
        folder = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        cmd = " ".join(f'""{a}""' if (" " in a or not a) else a for a in [str(exe)] + args)
        body = f'CreateObject("Wscript.Shell").Run "{cmd}", 0, False\r\n'
        return folder / "Tracd.vbs", body
    if sys.platform == "darwin":
        prog = "".join(f"<string>{escape(a)}</string>" for a in [str(py)] + args)
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.tracd</string>
<key>ProgramArguments</key><array>{prog}</array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
"""
        return Path.home() / "Library" / "LaunchAgents" / "com.tracd.plist", plist
    desktop = f"""[Desktop Entry]
Type=Application
Name=Tracd
Exec={" ".join(_desktop_quote(a) for a in [str(py)] + args)}
X-GNOME-Autostart-enabled=true
"""
    return Path.home() / ".config" / "autostart" / "tracd.desktop", desktop


def cmd_autostart(opts: argparse.Namespace) -> int:
    path, body = _startup_target(opts)
    if opts.state == "on":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        print(f"Tracd will start when you sign in ({path}).")
    else:
        if path.exists():
            path.unlink()
        print("Tracd will no longer start by itself.")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="where to keep data (default: %%APPDATA%%\\Tracd or ~/.tracd)")
    browser = argparse.ArgumentParser(add_help=False)
    browser.add_argument("--show-browser", action="store_true", help="keep the tracker's browser window visible")
    browser.add_argument("--headless", action="store_true", help="no browser window at all (shops block this more often)")
    browser.add_argument("--browser-path", default="", help="use this Chrome/Edge/Chromium executable")

    ap = argparse.ArgumentParser(prog="tracd", description="Tracd: never miss a restock again.", parents=[common, browser])
    ap.add_argument("--version", action="version", version=f"Tracd {__version__}")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--lan", action="store_true", help="also open the dashboard to your phone on the same Wi-Fi (with a key)")
    ap.add_argument("--no-open", action="store_true", help="don't open the dashboard in your browser")
    ap.add_argument("--demo", action="store_true", help="try the app with simulated shops")
    ap.add_argument("--quiet", action="store_true", help="log to the log file only")
    ap.set_defaults(func=cmd_run)
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", parents=[common, browser], help="read one product link and list its sizes")
    c.add_argument("link")
    c.add_argument("--size", help="also give the stock answer for this size, e.g. 'UK 9'")
    c.add_argument("--save", help="save the raw shop responses to this folder (for bug reports)")
    c.set_defaults(func=cmd_check)

    t = sub.add_parser("test-alert", parents=[common], help="send a test alert to your phone")
    t.set_defaults(func=cmd_test_alert)

    a = sub.add_parser("autostart", help="start Tracd when you sign in (with the options given here)")
    a.add_argument("state", choices=["on", "off"])
    # Also accepted after "autostart"; SUPPRESS keeps ones given before it.
    a.add_argument("--home", default=argparse.SUPPRESS)
    a.add_argument("--port", type=int, default=argparse.SUPPRESS)
    a.add_argument("--browser-path", default=argparse.SUPPRESS)
    for flag in ("--lan", "--show-browser", "--headless"):
        a.add_argument(flag, action="store_true", default=argparse.SUPPRESS)
    a.set_defaults(func=cmd_autostart)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass
    ap = build_parser()
    opts = ap.parse_args(argv)
    for k, v in (("port", DEFAULT_PORT), ("lan", False), ("no_open", False), ("demo", False), ("quiet", False)):
        if not hasattr(opts, k):
            setattr(opts, k, v)
    return opts.func(opts)


if __name__ == "__main__":
    sys.exit(main())
