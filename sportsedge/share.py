"""Reach the dashboard from anywhere:  .\\sportsedge share

What it does, in order:
  1. makes sure the dashboard is running on this computer (starts it if it is not);
  2. opens a secure outbound tunnel to it (Cloudflare quick tunnel: no account, no router changes);
  3. points a permanent https://<name>.vercel.app address at that tunnel, by deploying a tiny Vercel
     project that contains nothing but a forwarding rule;
  4. keeps this computer awake while it runs, and repeats 2-3 if the tunnel drops.

All the work still happens on this computer, so it has to stay on with this command running. Everything
that arrives from outside must get past the passcode (.\\sportsedge passcode); until one is set,
the address shows a "locked" page and nothing else.

Needs two free tools on the PATH: cloudflared, and the Vercel CLI signed in to your account (vercel login).
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

import requests

from . import access, config, tracker

FRONT = config.ROOT / "remote"  # the Vercel project: a forwarding rule and nothing else
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
VERCEL_URL = re.compile(r"https://[a-z0-9.-]+\.vercel\.app")


def _say(msg: str):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def _keep_awake(on: bool):
    """Stop Windows from sleeping while the dashboard is being shared (the screen may still turn off)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:
        pass


def _local_ok(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/status", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def _gate_ok(port: int) -> bool:
    """The running dashboard must refuse a request that looks like it came from outside. An older copy
    without the passcode gate must never be put on the internet."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/api/status", headers={"X-Forwarded-For": "203.0.113.1"}, timeout=5)
        return r.status_code in (401, 403) and r.headers.get("X-Sportsedge") == "1"
    except requests.RequestException:
        return False


def _start_dashboard(port: int):
    from . import web

    threading.Thread(target=web.serve, kwargs={"port": port, "open_browser": False}, daemon=True).start()
    for _ in range(40):
        if _local_ok(port):
            return
        time.sleep(0.5)
    raise RuntimeError("The dashboard did not start.")


class Tunnel:
    def __init__(self, port: int):
        exe = shutil.which("cloudflared")
        if not exe:
            raise RuntimeError("cloudflared is not installed. Install it with:  winget install Cloudflare.cloudflared")
        self.url = None
        self.lines: list[str] = []
        self.proc = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines = (self.lines + [line.rstrip()])[-40:]
            m = TUNNEL_URL.search(line)
            if m and not self.url:
                self.url = m.group(0)

    def wait(self, seconds: float = 45) -> str:
        t0 = time.time()
        while time.time() - t0 < seconds:
            if self.url:
                return self.url
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        raise RuntimeError("The tunnel did not come up:\n  " + "\n  ".join(self.lines[-8:]))

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def _vercel(args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    if not shutil.which("vercel"):
        raise RuntimeError("The Vercel CLI is not installed. Install it with:  npm install -g vercel   then run:  vercel login")
    return subprocess.run(f"vercel {args}", cwd=FRONT, shell=True, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def _project_name() -> str:
    """A neutral, hard-to-guess project name, chosen once. It becomes the address: <name>.vercel.app"""
    f = FRONT / "project-name.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    name = f"sportsedge-{secrets.token_hex(3)}"
    f.write_text(name, encoding="utf-8")
    return name


def _is_ours(url: str) -> bool:
    try:
        return requests.get(url, timeout=20, allow_redirects=False).headers.get("X-Sportsedge") == "1"
    except requests.RequestException:
        return False


def point_vercel_at(tunnel_url: str) -> str:
    """Deploy the forwarding rule and return the permanent address."""
    FRONT.mkdir(exist_ok=True)
    name = _project_name()
    (FRONT / "vercel.json").write_text(json.dumps({
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "rewrites": [{"source": "/(.*)", "destination": f"{tunnel_url}/$1"}],
        "headers": [{"source": "/(.*)", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}]}],
    }, indent=2), encoding="utf-8")
    (FRONT / "README.txt").write_text(
        "This folder is the whole Vercel project: one rule that forwards every request to the dashboard\n"
        "running on your own computer. `sportsedge share` rewrites vercel.json and redeploys it.\n", encoding="utf-8")
    if not (FRONT / ".vercel" / "project.json").exists():
        r = _vercel(f"link --yes --project {name}")
        if r.returncode != 0:
            raise RuntimeError("Could not create the Vercel project:\n" + (r.stderr or r.stdout)[-600:])
    r = _vercel("deploy --prod --yes")
    if r.returncode != 0:
        msg = (r.stderr or "") + (r.stdout or "")
        if "outdated" in msg.lower():
            raise RuntimeError("Vercel refused the deploy because the Vercel CLI on this computer is out of date.\n"
                               "Update it with:  npm i -g vercel@latest   then run this command again.")
        if "credentials" in msg.lower() or "not authenticated" in msg.lower():
            raise RuntimeError("The Vercel CLI is not signed in. Run:  vercel login   then run this command again.")
        raise RuntimeError("The Vercel deploy failed:\n" + msg[-800:])
    found = VERCEL_URL.findall(r.stdout + "\n" + r.stderr)
    deployment = found[-1] if found else None
    # the permanent address is the project's production alias; fall back to this deployment's own address
    candidates = [f"https://{name}.vercel.app"]
    if deployment:
        insp = _vercel(f"inspect {deployment}", timeout=60)
        candidates += [u for u in VERCEL_URL.findall(insp.stdout + "\n" + insp.stderr) if u != deployment]
        candidates.append(deployment)
    for _ in range(3):
        for url in dict.fromkeys(candidates):
            if _is_ours(url):
                return url
        time.sleep(4)
    raise RuntimeError(f"Deployed, but none of these addresses reached the dashboard: {', '.join(dict.fromkeys(candidates))}")


def _beat(url: str, tunnel: str):
    try:
        (tracker.history_dir() / "share.json").write_text(json.dumps({"url": url, "tunnel": tunnel, "beat": time.time()}), encoding="utf-8")
    except Exception:
        pass


RETRY_SECONDS = 180  # how long to wait before trying to share again after it fails


def _forget_share():
    """The dashboard reads this file to show the outside address: without a working one there is none to show."""
    try:
        (tracker.history_dir() / "share.json").unlink()
    except Exception:
        pass


def run(port: int = 8765):
    if _local_ok(port):
        _say(f"Using the dashboard already running on this computer (port {port}).")
    else:
        _say("Starting the dashboard ...")
        _start_dashboard(port)
    if not _gate_ok(port):
        raise SystemExit("The dashboard running on this computer is an older copy without the passcode gate.\n"
                         "Close it, then run this command again. Nothing was shared.")
    locked = not access.has_passcode()
    tunnel = None
    _keep_awake(True)
    try:
        while True:
            try:
                _say("Opening a secure tunnel ...")
                tunnel = Tunnel(port)
                t_url = tunnel.wait()
                _say("Pointing your Vercel address at it (about 20 seconds) ...")
                url = point_vercel_at(t_url)
                _beat(url, t_url)
                print("\n" + "=" * 68)
                print(f"  Your dashboard, from anywhere:   {url}")
                if locked:
                    print("  It is LOCKED: no passcode is set, so it shows a locked page to everyone.")
                    print("  In another terminal, in this folder, run:   .\\sportsedge passcode")
                else:
                    print("  Sign in with your passcode. Sessions last 30 days per device.")
                print("  Keep this window open. Press Ctrl+C to stop sharing.")
                print("=" * 68 + "\n", flush=True)
                while tunnel.alive():
                    time.sleep(20)
                    _beat(url, t_url)
                    if locked and access.has_passcode():
                        locked = False
                        _say("Passcode set: remote access is open to anyone who knows it.")
                _say("The tunnel dropped. Reconnecting ...")
                time.sleep(3)
            except RuntimeError as e:
                # The outside address is an extra. A tunnel that will not open or a deploy that is refused (both
                # happen when this is restarted several times in a few minutes) must not take the dashboard on
                # this computer down with it: it keeps running here, and sharing is tried again.
                print(f"\nCould not share the dashboard just now. It is still running on this computer: http://127.0.0.1:{port}/\n{e}", flush=True)
                if tunnel:
                    tunnel.stop()
                    tunnel = None
                _forget_share()
                _say(f"Trying to share again in {RETRY_SECONDS // 60} minutes. Press Ctrl+C to stop.")
                time.sleep(RETRY_SECONDS)
    except KeyboardInterrupt:
        _say("Stopped sharing. The address now shows an error page until you run this again.")
    finally:
        _keep_awake(False)
        if tunnel:
            tunnel.stop()
        _forget_share()
