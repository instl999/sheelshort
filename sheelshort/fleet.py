"""AutoDL.Art app instances as ComfyUI servers: create, power on/off, release, and find their ComfyUI address.

The user's servers are instances of an AutoDL.Art application (ComfyUI with MiniMax H3 and the other models baked
in), so an identical server is one `create` call with the same application and version. API docs:
https://www.autodl.art/docs/app_api/ . The developer token is read from $AUTODL_TOKEN or
~/.config/sheelshort/autodl_token and never written anywhere else.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

BASE = "https://www.autodl.art"
TOKEN_FILE = Path.home() / ".config" / "sheelshort" / "autodl_token"


class AutoDLError(RuntimeError):
    pass


def token() -> str:
    t = os.environ.get("AUTODL_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    if not t:
        raise AutoDLError(f"no AutoDL developer token: set AUTODL_TOKEN or write it to {TOKEN_FILE} (chmod 600)")
    return t


class AutoDL:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, headers={"Authorization": token()}, timeout=30)

    def _call(self, method: str, path: str, **kw) -> object:
        for attempt in range(4):
            try:
                r = self.http.request(method, f"/api/v1/adl_dev/dev/instance/pro/{path}", **kw)
                break
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        j = r.json()
        if j.get("code") != "Success":
            raise AutoDLError(f"{path}: {j.get('code')} {j.get('msg')}")
        return j.get("data")

    def create(self, application_uuid: str, version: str, gpu_spec: str, name: str, data_centers: list[str] | None = None,
               disk_gb: int = 30) -> str:
        body = {"req_gpu_amount": 1, "expand_system_disk_by_gb": disk_gb, "gpu_spec_uuid": gpu_spec,
                "application_uuid": application_uuid, "application_version": version, "instance_name": name}
        if data_centers:
            body["data_center_list"] = data_centers
        return self._call("POST", "create", json=body)

    def status(self, uuid: str) -> str:
        return self._call("GET", "status", params={"instance_uuid": uuid})

    def snapshot(self, uuid: str) -> dict:
        return self._call("GET", "snapshot", params={"instance_uuid": uuid})

    def instances(self) -> list[dict]:
        return (self._call("POST", "list", json={"page_index": 1, "page_size": 100}) or {}).get("list") or []

    def power_on(self, uuid: str) -> None:
        self._call("POST", "power_on", json={"instance_uuid": uuid, "payload": "gpu"})

    def power_off(self, uuid: str) -> None:
        self._call("POST", "power_off", json={"instance_uuid": uuid})

    def release(self, uuid: str) -> None:
        self._call("POST", "release", json={"instance_uuid": uuid})

    def comfy_url(self, uuid: str) -> str:
        """The instance's ComfyUI address. The app serves ComfyUI on 6006 on some instances and 6008 on others, so
        both public addresses are tried and the one that answers /system_stats wins ("" while neither answers).
        The public proxy on :8443 is HTTPS whatever the in-container protocol."""
        s = self.snapshot(uuid)
        for key in ("service_6006_domain", "service_6008_domain"):
            if s.get(key) and is_comfy(f"https://{s[key]}", timeout=10):
                return f"https://{s[key]}"
        return ""

    def wait_status(self, uuid: str, want: str, timeout_s: float = 1800, poll_s: float = 5) -> float:
        """Seconds until the instance reports `want`."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.status(uuid) == want:
                return time.time() - t0
            time.sleep(poll_s)
        raise TimeoutError(f"{uuid} not {want} after {timeout_s}s (last: {self.status(uuid)})")


def is_comfy(url: str, timeout: float = 15) -> bool:
    """True only if ComfyUI itself answers at `url`: its /system_stats JSON lists the GPUs. The port ComfyUI uses
    differs between boots, and the app's other port may answer any path with an HTML page and status 200; taking that
    for ComfyUI put two servers in the pool that never got a job (2026-10-31, E03/E04)."""
    try:
        r = httpx.get(f"{url}/system_stats", timeout=timeout)
        return r.status_code == 200 and bool(r.json().get("devices"))
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def wait_comfy(url: str, timeout_s: float = 1800, poll_s: float = 5) -> float:
    """Seconds until ComfyUI at `url` answers /system_stats."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if is_comfy(url):
            return time.time() - t0
        time.sleep(poll_s)
    raise TimeoutError(f"ComfyUI at {url} not answering after {timeout_s}s")


OWNED = Path(os.environ.get("SHEELSHORT_FLEET_OWNED") or Path(__file__).resolve().parent.parent / ".cache" / "fleet_owned.json")


# ---------- holders: which sheelshort processes use which instance ----------
# OWNED maps an instance to the live processes using it ("pids") and, once none is left, when it went idle ("since").
# Several sheelshort commands can run at once (two projects, say) and share servers: a server is powered off only when
# no live process holds it.

@contextmanager
def _registry():
    import fcntl
    import json
    OWNED.parent.mkdir(parents=True, exist_ok=True)
    with open(OWNED.with_name(OWNED.name + ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        raw = OWNED.read_text() if OWNED.exists() else ""
        d = json.loads(raw) if raw.strip() else {}
        d = {u: e if isinstance(e, dict) else {"pids": [e]} for u, e in d.items()}   # the old format: uuid -> pid
        yield d
        OWNED.write_text(json.dumps(d))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _live(pids) -> list[int]:
    return [q for q in pids if q == os.getpid() or _alive(q)]


def hold(uuid: str, url: str = "", claim: bool = False) -> bool:
    """This process uses the instance. With `claim`, only if no live process holds it yet (for powering one on:
    two runs must not both start the same stopped server). False when the claim failed."""
    with _registry() as d:
        e = d.setdefault(uuid, {"pids": []})
        e["pids"] = _live(e.get("pids", []))
        if claim and e["pids"]:
            return False
        e["pids"] = sorted({*e["pids"], os.getpid()})
        e.pop("since", None)
        if url:
            e["url"] = url
        return True


def let_go(uuid: str) -> bool:
    """This process no longer uses the instance. True when no other live process does: it is idle from now."""
    with _registry() as d:
        e = d.get(uuid)
        if e is None:
            return True
        e["pids"] = [q for q in _live(e.get("pids", [])) if q != os.getpid()]
        if e["pids"]:
            return False
        e["since"] = time.time()
        return True


def forget(uuid: str) -> None:
    with _registry() as d:
        d.pop(uuid, None)


def holders(uuid: str) -> list[int]:
    with _registry() as d:
        return _live(d.get(uuid, {}).get("pids", []))


def ours(c: dict, inst: dict) -> bool:
    """An instance this engine may power on and off: a core server, or one it created (`name_prefix`-N), in the
    allowed regions."""
    return (inst.get("uuid") in (c.get("core") or []) or (inst.get("name") or "").startswith(c["name_prefix"])) and \
        any(r in (inst.get("region_name") or "") for r in c.get("region_names", ["北京"]))


def queue_idle(url: str) -> bool:
    try:
        r = httpx.get(f"{url}/queue", timeout=15)
        q = r.json()
        return not q.get("queue_running") and not q.get("queue_pending")
    except (httpx.HTTPError, ValueError, AttributeError):
        return True   # ComfyUI not answering: nothing is running there


def _stopped_for(inst: dict, clock=time.time) -> float | None:
    """Seconds an instance has been shut down, or None while it is not shut down."""
    from datetime import datetime
    if inst.get("status") != "shutdown":
        return None
    st = inst.get("stopped_at") or {}
    when = st.get("Time") if isinstance(st, dict) and st.get("Valid", True) else None
    if not when:
        return None
    try:
        return max(0.0, clock() - datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def reap_idle(api: "AutoDL", c: dict, log=print, clock=time.time, idle=queue_idle) -> float | None:
    """Power off every instance of ours that no live sheelshort process holds and whose ComfyUI queue has been empty
    for `idle_off_minutes` (a queue with work, from the ComfyUI page say, restarts the wait), and release (delete) a
    `name_prefix`-N one that has been shut down for `release_after_hours` (3; 0 = never), so idle instances cost nothing.
    A core server is only ever powered off, never released. A running core server
    with no record is idle from now; a process that died without cleaning up counts as gone. Returns the seconds until
    the next check is due, None when nothing is left to watch."""
    idle_s = 60 * float(c.get("idle_off_minutes", 10))
    insts = {i["uuid"]: i for i in api.instances() if ours(c, i)}
    nxt = None

    def due_in(s):
        nonlocal nxt
        nxt = s if nxt is None else min(nxt, s)

    hours = float(c.get("release_after_hours", 3))
    for u, inst in insts.items() if hours > 0 else []:
        if not (inst.get("name") or "").startswith(c["name_prefix"]):
            continue   # only instances this engine created are ever released: core servers are the owner's
        stopped = _stopped_for(inst, clock)
        if stopped is None or holders(u):
            continue
        left = hours * 3600 - stopped
        if left > 0:
            due_in(left)   # keep watching until it is due
            continue
        try:
            api.release(u)
            forget(u)
            log(f"fleet: {u}: released (shut down for {stopped / 3600:.1f} h)")
        except AutoDLError as err:
            log(f"fleet: {u}: release failed ({err}); trying again later")
            due_in(600)

    with _registry() as d:
        for u in c.get("core") or []:
            if u not in d and insts.get(u, {}).get("status") == "running":
                d[u] = {"pids": [], "since": clock()}
        for u, e in list(d.items()):
            e["pids"] = _live(e.get("pids", []))
            status = insts.get(u, {}).get("status")
            if e["pids"]:
                due_in(120)   # in use: keep watching, the run may die without letting go
                continue
            if status in (None, "shutdown"):
                d.pop(u)
                continue
            if status != "running":   # starting or stopping: look again soon
                due_in(60)
                continue
            e.setdefault("since", clock())
            left = e["since"] + idle_s - clock()
            if left > 0:
                due_in(left)
                continue
            url = e.get("url") or api.comfy_url(u)
            if url and not idle(url):
                e["since"] = clock()
                due_in(idle_s)
                log(f"fleet: {u} has work queued outside sheelshort; left on")
                continue
            try:   # under the lock: a run that wants this server waits, then finds it stopped and powers it on again
                api.power_off(u)
                d.pop(u)
                log(f"fleet: {u}: powered off (idle {(clock() - e['since']) / 60:.0f} min, no run uses it)")
            except AutoDLError as err:
                log(f"fleet: {u}: power off failed ({err}); trying again later")
                due_in(120)
    return nxt


REAPER_PID = OWNED.with_name("fleet_reaper.pid")


STOCK = ("暂无库存", "服务正忙", "ServerBusy", "库存")   # AutoDL's "no GPU free right now": worth asking again at once


def grab(api, uuids: list[str], c: dict, log, claim: bool = True, stop=None) -> str | None:
    """Power on the first of `uuids` AutoDL will start. A GPU often frees up within seconds, so a "no stock" or "busy"
    answer is retried: every server in turn, `grab_rounds` (6) rounds `grab_gap_s` (10) apart."""
    rounds, gap = int(c.get("grab_rounds", 6)), float(c.get("grab_gap_s", 10))
    last = ""
    for r in range(rounds):
        busy = False   # only a "no stock" answer is worth another round
        for u in uuids:
            if claim and not hold(u, claim=True):   # another run is starting it
                continue
            try:
                api.power_on(u)
                log(f"{u}: powered on" + (f" (round {r + 1})" if r else ""))
                return u
            except AutoDLError as e:
                if claim:
                    let_go(u)
                last = str(e)
                if any(k in last for k in STOCK):
                    busy = True
                else:
                    log(f"{u}: power on failed ({e})")
        if not busy or (stop is not None and stop.is_set()):
            break
        if r < rounds - 1:
            time.sleep(gap)
    if last:
        log(f"no GPU free after {rounds} rounds: {last[:80]}")
    return None


CAPS = Path(__file__).resolve().parent.parent / ".cache" / "server_caps.json"


_caps_lock = threading.Lock()


def _caps() -> dict:
    try:
        return json.loads(CAPS.read_text())
    except (OSError, ValueError):
        return {}


def power_key(inst: dict) -> str:
    """One power-on of an instance: its uuid and when it last started. A power cycle can put an instance on another
    GPU, so what it can do is known only for the power-on it was seen in."""
    started = inst.get("started_at") or {}
    return f"{inst['uuid']}@{started.get('Time', '') if isinstance(started, dict) else started}"


def rtx_known(key: str) -> bool | None:
    """True: this power-on ran RTX upscaling; False: it could not; None: not tried yet."""
    v = _caps().get("rtx", {}).get(key)
    return v if isinstance(v, bool) else None


def note_rtx(key: str, ok: bool) -> None:
    with _caps_lock:
        c = _caps()
        c["rtx"] = {k: v for k, v in c.get("rtx", {}).items() if isinstance(v, bool)}
        c["rtx"][key] = ok
        for old in ("no_rtx_vsr", "no_rtx_vsr_machines", "rtx_vsr_ok_machines"):   # per machine: wrong (GPUs move)
            c.pop(old, None)
        CAPS.parent.mkdir(parents=True, exist_ok=True)
        CAPS.write_text(json.dumps(c, indent=1))


def probe_rtx(p, url: str) -> bool | None:
    """Can the server at `url` run RTX upscaling? A one-second test clip, upscaled there: True, False (the NVIDIA
    effects runtime does not load: an old driver), or None (the probe could not run: a network error, say)."""
    import subprocess
    from . import flows
    # exactly what a real upscale is: an H3 take's size and frame rate, at the scale the edit asks for (a tiny clip at
    # 2x once failed on a machine that upscaled real takes fine)
    w, h, fps = 832, 1504, 24
    e = p.cfg["edit"]
    scale = max(e["width"] / w, e["height"] / h) + 0.005
    clip = CAPS.parent / f"rtx_probe_{w}x{h}.mp4"
    if not clip.exists():
        CAPS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d=1:r={fps}", "-f", "lavfi",
                        "-i", "anullsrc", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                       check=True)
    key = f"probe/rtx/{url.split('//')[-1].split('.')[0]}_{int(time.time())}"
    out = p.run_job(key, lambda cl: flows.upscale_rtx(p.put(cl, clip), scale, "sheelshort/rtx_probe"),
                    CAPS.parent / "probe", "rtx_probe", ("gifs", "videos"), 300, only=url)   # on that server and no other
    for f in out or []:
        f.unlink(missing_ok=True)
    job = p.state["jobs"].get(key, {})
    if job.get("server") != url:
        return None   # it did not run there: no verdict about this machine
    if out:
        return True
    return False if "NvVFX" in job.get("error", "") else None


def uuid_for(url: str) -> str | None:
    """The instance whose ComfyUI answers at `url` (from the holder registry)."""
    with _registry() as d:
        return next((u for u, e in d.items() if e.get("url") and e["url"].rstrip("/") == url.rstrip("/")), None)


def spawn_reaper(project_root) -> None:
    """Start the background watcher that powers idle servers off (`fleet <project> idle`), unless one is running."""
    import subprocess
    import sys
    try:
        if _alive(int(REAPER_PID.read_text())):
            return
    except (OSError, ValueError):
        pass
    repo = Path(__file__).resolve().parent.parent
    out = open(OWNED.with_name("fleet_reaper.log"), "a")   # noqa: SIM115 - handed to the child
    proc = subprocess.Popen([sys.executable, "-m", "sheelshort", "fleet", str(project_root), "idle"], cwd=repo,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    REAPER_PID.write_text(str(proc.pid))


def watch_idle(api: "AutoDL", c: dict, log=print) -> None:
    """The reaper: check until every idle server is powered off (or taken by a new run), then exit."""
    REAPER_PID.parent.mkdir(parents=True, exist_ok=True)
    REAPER_PID.write_text(str(os.getpid()))
    while True:
        try:
            nxt = reap_idle(api, c, log)
        except Exception as e:  # noqa: BLE001 - a network hiccup must not leave servers running unwatched
            log(f"fleet: idle check failed ({e}); retrying")
            nxt = 60
        if nxt is None:
            return
        time.sleep(max(15.0, min(nxt + 1, 120.0)))


def bring_up(api: "AutoDL", uuid: str, stop: threading.Event | None = None, clock=time.time) -> str:
    """Wait for a powered-on instance to run and its ComfyUI to answer; its address ("" if it never did)."""
    t0 = clock()
    api.wait_status(uuid, "running", timeout_s=900)
    url = ""
    while not url and clock() - t0 < 1200 and not (stop and stop.is_set()):
        url = api.comfy_url(uuid)
        if not url:
            time.sleep(10)
    return url


class Session:
    """The servers one sheelshort command runs on, when every server is powered on on demand (`[fleet] core`).

    Nothing is started until a job needs a GPU (`ensure`, called by `Project.pick`): then every instance of ours that
    is already running is used as it is (warm, and billed anyway), and if that makes fewer than `min_servers`, stopped
    core servers are powered on, then stopped fleet servers, then a new one is created. While a render runs the Fleet
    adds more. When the command ends (`release`) its servers are let go; a server no process holds is powered off by
    the reaper once its queue has been empty for `idle_off_minutes`, so back-to-back commands find it still warm."""

    def __init__(self, p, api: AutoDL | None = None, spawn=spawn_reaper):
        self.p, self.c = p, p.cfg["fleet"]
        self.api = api if api is not None else AutoDL()
        self.spawn = spawn
        self.held: dict[str, str] = {}   # uuid -> ComfyUI address
        self.lock = threading.Lock()
        self.swap_lock = threading.Lock()   # one machine swap at a time (never held around ensure's fast path)
        self.vetting: set[str] = set()      # uuids being probed right now
        self.woken = False
        self.tried_at = 0.0

    def ensure(self) -> None:
        """Wake the servers on the first GPU job; while none could start (AutoDL out of stock: "暂无库存"), try again
        every `stock_retry_s` (60) instead of giving the run up."""
        with self.lock:
            now = time.time()
            if self.woken and (self.p.cfg["comfy"]["servers"] or now - self.tried_at < float(self.c.get("stock_retry_s", 60))):
                return
            if self.woken:
                self.log("no server is up yet; trying to start one again")
            self.woken, self.tried_at = True, now
            try:
                self._wake()
            except Exception as e:  # noqa: BLE001
                self.log(f"waking servers failed: {e}")
            if self.held and not self.p.dry:
                self.spawn(self.p.root)   # from the start: a run killed outright (power loss, kill -9) still gets its
                                          # servers powered off once idle

    def _wake(self) -> None:
        servers = self.p.cfg["comfy"]["servers"]
        insts = {i["uuid"]: i for i in self.api.instances() if ours(self.c, i)}
        core = [u for u in self.c.get("core") or [] if u in insts]
        order = core + sorted(u for u in insts if u not in core)
        with self.p.lock:   # a configured address of an on-demand server may be stale (6006/6008): resolved below
            servers[:] = [s for s in servers if not any(u.split("-", 1)[-1] in s for u in order)]
        cap = int(self.c.get("max_servers", 4))
        for u in order:
            if insts[u].get("status") == "running" and len(servers) < cap:
                hold(u)
                url = self.api.comfy_url(u)
                if url:
                    self._add(u, url)
                    self.log(f"{u}: already running -> in the pool")
                else:
                    let_go(u)
        need = min(int(self.c.get("min_servers", 1)), cap) - len(servers)
        if need <= 0 or self.p.dry:
            return
        started = []
        stopped = [u for u in order if insts[u].get("status") == "shutdown"]
        while len(started) < need:
            u = grab(self.api, [x for x in stopped if x not in started], self.c, self.log)
            if not u:
                break
            started.append(u)
        while len(started) < need:   # nothing stopped could start: create one (the Fleet's rule, same app and region)
            u = Fleet(self.p, api=self.api)._create()
            if not u:
                break
            hold(u)
            started.append(u)
        threads = [threading.Thread(target=self._up, args=(u,)) for u in started]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if not servers:
            self.log("no server could be started")

    def power_keys(self) -> dict[str, str]:
        """Pool address -> the power-on it is in (`power_key`): what a server can do is judged per power-on."""
        try:
            insts = {i["uuid"]: i for i in self.api.instances()}
        except Exception:  # noqa: BLE001
            return {}
        out = {}
        for url in list(self.p.cfg["comfy"]["servers"]):
            u = next((x for x, v in self.held.items() if v == url), None) or uuid_for(url)
            if u in insts:
                out[url] = power_key(insts[u])
        return out

    def needs_rtx(self) -> bool:
        return (self.p.cfg["edit"].get("upscale", "rtx") == "rtx" and bool(self.c.get("probe_rtx", True))
                and not self.p.dry)

    def vet_later(self, u: str, url: str) -> None:
        """Probe a server that just joined the pool for RTX upscaling, in the background; an old-driver machine is
        replaced (see `replace`)."""
        if self.needs_rtx() and u not in self.vetting:
            threading.Thread(target=self._vet, args=(u, url), daemon=True).start()

    def _vet(self, u: str, url: str) -> None:
        self.vetting.add(u)
        try:
            key = self.power_keys().get(url, "")
            ok = rtx_known(key) if key else None
            if ok is None:
                ok = probe_rtx(self.p, url)
                if ok is None or not key:
                    return
                note_rtx(key, ok)
                self.log(f"{u}: " + ("runs RTX upscaling" if ok else "cannot run RTX upscaling on this power-on (its GPU's driver)"))
            if not ok:
                self.replace(u, url)
        except Exception as e:  # noqa: BLE001 - vetting must never break the run
            self.log(f"{u}: vetting failed ({e})")
        finally:
            self.vetting.discard(u)

    def capable_urls(self) -> set[str]:
        """Pool servers proven, in their current power-on, to run RTX upscaling (one not tested yet does not count)."""
        return {url for url, k in self.power_keys().items() if rtx_known(k) is True}

    def replace(self, u: str | None = None, url: str | None = None) -> bool:
        """Swap an old-driver machine for one that runs RTX upscaling, acquiring first: a new machine is brought in
        and probed (one over `max_servers` for the moment), and only once it passes is the old one powered off (after
        its job in hand), so it is not simply started again. A new machine that fails is powered off at once. Bounded
        by `replace_window_minutes` (20) and `replace_max_new` (3) machines tried. `u`/`url` None: just make sure a
        capable server is in the pool, retiring an idle old-driver one after. Returns True when a capable server is
        in the pool."""
        with self.swap_lock:
            t0 = time.time()   # a server still being probed may be the capable one: wait for its verdict first
            while self.vetting - {u} and not self.capable_urls() - ({url} if url else set()) and time.time() - t0 < 180:
                time.sleep(5)
            if self.capable_urls() - ({url} if url else set()):
                got = True   # another swap (or the vetting of a joining server) found one meanwhile
            else:
                got = self._acquire_capable()
            if not got:
                self.log("no machine that can run RTX upscaling found in the window; keeping the old-driver servers for "
                         "rendering")
                return False
            if url is None:   # retire one idle server of this run that cannot upscale, if there is one
                keys = self.power_keys()
                u, url = next(((x, v) for x, v in self.held.items() if v and rtx_known(keys.get(v, "")) is False), (None, None))
            if u and url:
                self._retire_when_idle(u, url)
            return True

    def workload_minutes(self) -> float:
        """GPU minutes of work this run still has queued: the renders left (the render's fleet counts them) at
        `clip_minutes` each, plus a minute per upscale or check not done yet."""
        scaler = getattr(self.p, "scaler", None)
        renders = scaler.pending if scaler else 0
        waiting = sum(1 for k, j in list(self.p.state.get("jobs", {}).items())
                      if j.get("status") in ("queued", "retrying") and not k.startswith("render/"))
        return renders * float(self.c.get("clip_minutes", 5)) + waiting

    def replace_budget(self) -> tuple[float, int]:
        """(seconds, new servers) a replacement may spend. The base (`replace_window_minutes` 20, `replace_max_new`
        3) grows with the work queued: one more server per 30 GPU minutes, up to `replace_max_new_heavy` (10) servers
        and `replace_window_minutes_heavy` (60): a busy run is worth more tries, a run nearly done is not."""
        work = self.workload_minutes()
        n = int(self.c.get("replace_max_new", 3)) + int(work // 30)
        n = min(n, int(self.c.get("replace_max_new_heavy", 10)))
        minutes = min(float(self.c.get("replace_window_minutes", 20)) + work / 3,
                      float(self.c.get("replace_window_minutes_heavy", 60)))
        return 60 * minutes, n

    def _acquire_capable(self) -> bool:
        """Acquire servers until one runs RTX upscaling. A new one that cannot stays on until a capable one is in (so
        AutoDL cannot hand the same GPU straight back), then every such one is powered off."""
        window, max_new = self.replace_budget()
        if max_new > int(self.c.get("replace_max_new", 3)):
            self.log(f"heavy work queued (~{self.workload_minutes():.0f} GPU min): up to {max_new} new servers in "
                     f"{window / 60:.0f} min to find one that runs RTX upscaling")
        t0, failed, found = time.time(), [], False
        try:
            while time.time() - t0 < window and len(failed) < max_new:
                try:
                    stopped = [i["uuid"] for i in self.api.instances() if ours(self.c, i) and i.get("status") == "shutdown"
                               and i["uuid"] not in self.held]
                except Exception as e:  # noqa: BLE001
                    self.log(f"listing servers failed: {e}")
                    stopped = []
                u = grab(self.api, stopped, self.c, self.log) if stopped else None
                if not u:
                    u = Fleet(self.p, api=self.api)._create()
                    if u:
                        hold(u)
                if not u:   # no stock: wait a minute, inside the window
                    time.sleep(60)
                    continue
                self.vetting.add(u)   # probed here, not by the background vetting
                try:
                    self._up(u)
                    url = self.held.get(u)
                    if not url:
                        continue
                    key = self.power_keys().get(url, "")
                    ok = probe_rtx(self.p, url)
                    if key and ok is not None:
                        note_rtx(key, ok)
                    if ok:
                        self.log(f"{u}: runs RTX upscaling; in the pool")
                        found = True
                        return True
                    self.log(f"{u}: cannot run RTX upscaling either; kept on until a capable server is in")
                    failed.append((u, url))
                finally:
                    self.vetting.discard(u)
            return False
        finally:
            for u, url in failed:
                self._drop(u, url, "cannot run RTX upscaling" + ("; a capable server replaced it" if found else ""))

    def _retire_when_idle(self, u: str, url: str, wait_s: float = 900) -> None:
        """Power off an old-driver server once the job it is running is done (at most `wait_s`)."""
        t0 = time.time()
        while self.p._assigned.get(url) and time.time() - t0 < wait_s:
            time.sleep(20)
        scaler = getattr(self.p, "scaler", None)
        if scaler and u in scaler.managed:
            scaler._retire(u, "old driver: replaced by a machine that runs RTX upscaling", force=True)
        else:
            self._drop(u, url, "old driver: replaced by a machine that runs RTX upscaling")

    def _drop(self, u: str, url: str, why: str) -> None:
        self.held.pop(u, None)
        with self.p.lock:
            if url in self.p.cfg["comfy"]["servers"]:
                self.p.cfg["comfy"]["servers"].remove(url)
        if let_go(u):
            try:
                self.api.power_off(u)
                forget(u)
                self.log(f"{u}: powered off ({why})")
            except AutoDLError as e:
                self.log(f"{u}: power off failed ({e})")
        else:
            self.log(f"{u}: out of this run's pool ({why}); another run still uses it")

    def _up(self, u: str) -> None:
        t0 = time.time()
        try:
            url = bring_up(self.api, u)
        except Exception as e:  # noqa: BLE001
            url = ""
            self.log(f"{u}: {e}")
        if url:
            self._add(u, url)
            self.log(f"{u}: in the pool after {time.time() - t0:.0f}s -> {url}")
        else:
            self.log(f"{u}: ComfyUI never answered; letting it go")
            self.held[u] = ""

    def _add(self, u: str, url: str) -> None:
        hold(u, url)
        self.held[u] = url
        with self.p.lock:
            if url not in self.p.cfg["comfy"]["servers"]:
                self.p.cfg["comfy"]["servers"].append(url)
        self.vet_later(u, url)

    def release(self) -> None:
        idle = [u for u in self.held if let_go(u)]
        if idle and not self.p.dry:
            self.log(f"{len(idle)} server(s) idle; powered off after {self.c.get('idle_off_minutes', 10)} min unless a new run takes them")
            self.spawn(self.p.root)
        self.held.clear()

    def log(self, msg: str) -> None:
        from .project import log
        log(f"fleet: {msg}")


class Fleet:
    """Grows and shrinks the render pool with more AutoDL.Art instances of the same app while a render runs, up to
    `max_servers` in all.

    Only core servers (`core`) and instances this engine named (`name_prefix`-…) in the allowed regions are ever
    touched; stopped core servers are powered on before fleet ones, and a new instance is created only when none can
    start. Every instance it started is powered off when the run ends (`stop`), and an idle one is drained and powered
    off earlier (`idle_off_minutes`), unless another sheelshort run holds it. With `dry_run` the decisions are only
    logged."""

    def __init__(self, p, api: AutoDL | None = None, clock=time.time):
        self.p, self.c = p, p.cfg["fleet"]
        self.dry = bool(self.c.get("dry_run", True))
        self.api = api if api is not None else AutoDL()
        self.clock = clock
        self.pending = 0
        self.linger_s = 0.0   # with no renders left, keep an idle server this long: QC and upscale jobs still come
        self.managed: dict[str, dict] = {}      # uuid -> {"url", "idle_since"}
        self.starting = False
        self.backoff_until = 0.0
        self.hold_pending: int | None = None   # after an idle power-off: no scale-up until the backlog drops below this
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._up: threading.Thread | None = None

    # ---------- run lifecycle ----------

    def start(self, jobs: int) -> None:
        self.pending = jobs
        self.log(f"start: {jobs} job(s), {len(self.p.cfg['comfy']['servers'])} server(s), up to {self.c['max_servers']} in all"
                 + (" [dry run]" if self.dry else ""))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def job_done(self) -> None:
        with self.lock:
            self.pending = max(0, self.pending - 1)

    def stop(self) -> None:
        """End of the run: power off every instance this run added."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._up and self._up.is_alive():   # a server still starting must not be left running (and billed)
            self.log("waiting for a starting server before powering everything off")
            self._up.join(timeout=1500)
        for uuid in list(self.managed):
            self._retire(uuid, "run finished", force=True)

    def _loop(self) -> None:
        while not self._stop.wait(float(self.c.get("tick_s", 20))):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - scaling must never break the render
                self.log(f"tick failed: {e}")

    # ---------- decisions ----------

    def tick(self) -> None:
        if self.hold_pending is not None and self.pending < self.hold_pending:
            self.hold_pending = None
        if (len(self.p.cfg["comfy"]["servers"]) < int(self.c["max_servers"]) and not self.starting and self.clock() >= self.backoff_until
                and self.hold_pending is None and self.gain_minutes() >= float(self.c.get("min_gain_minutes", 3))):
            self.starting = True
            self._up = threading.Thread(target=self._scale_up, daemon=True)
            self._up.start()
        for uuid, m in list(self.managed.items()):
            if not m["url"]:
                continue   # still starting
            busy = self._busy(m["url"])
            if busy is None:   # unreachable: counts as busy, never idle, but a wrong address must not stay in the pool
                m.setdefault("bad_since", self.clock())
                if self.clock() - m["bad_since"] >= 180:
                    self._re_resolve(uuid, m)
                busy = True
            else:
                m.pop("bad_since", None)
            if busy:
                m["idle_since"] = None
            elif m["idle_since"] is None:
                m["idle_since"] = self.clock()
            idle_for = 0 if m["idle_since"] is None else self.clock() - m["idle_since"]
            done = self.pending == 0 and idle_for >= self.linger_s
            if not busy and (done or idle_for >= 60 * float(self.c.get("idle_off_minutes", 10))):
                if self._retire(uuid, f"idle {idle_for / 60:.0f} min" if self.pending else "no work left") and self.pending:
                    self.hold_pending = self.pending   # work is waiting but this server got none: another would sit idle too

    def gain_minutes(self) -> float:
        """Minutes one more server would take off this run: with n servers working, W minutes of GPU work left and a
        startup cost of S minutes, (W - n*S) / (n*(n+1)). Positive only when the work left outlasts the startup."""
        n = max(1, len(self.p.cfg["comfy"]["servers"]))
        w = self.pending * float(self.c.get("clip_minutes", 5))
        s = float(self.c.get("startup_minutes", 5))
        return (w - n * s) / (n * (n + 1))

    def _busy(self, url: str) -> bool | None:
        """True if the server has work, False if idle, None if it does not answer as ComfyUI."""
        if self.p._assigned.get(url, 0) > 0:
            return True
        try:
            running, pending = self.p.client(url).queue_info()
            return running + pending > 0
        except Exception:  # noqa: BLE001
            return None

    def _re_resolve(self, uuid: str, m: dict) -> None:
        """A pool server that has not answered for minutes: look its ComfyUI address up again and swap it in."""
        m["bad_since"] = self.clock()
        url = self.api.comfy_url(uuid)
        if url and url != m["url"]:
            with self.p.lock:
                servers = self.p.cfg["comfy"]["servers"]
                servers[:] = [url if u == m["url"] else u for u in servers]
            self.log(f"{uuid}: {m['url']} does not answer as ComfyUI; now {url}")
            m["url"] = url

    # ---------- actions ----------

    def _scale_up(self) -> None:
        try:
            if self.dry:
                self.log(f"would add a server ({self.pending} job(s) for {len(self.p.cfg['comfy']['servers'])} server(s), "
                         f"saves ~{self.gain_minutes():.1f} min)")
                self.backoff_until = self.clock() + 600
                return
            uuid = self._power_on_stopped() or self._create()
            if not uuid:
                self.backoff_until = self.clock() + 60 * float(self.c.get("retry_minutes", 1))
                return
            self.managed[uuid] = {"url": "", "idle_since": None}   # owned from here on, so `stop` powers it off
            hold(uuid)                                             # and the reaper does if this run dies
            t0 = self.clock()
            url = bring_up(self.api, uuid, self._stop, self.clock)
            if not url:
                self.log(f"{uuid}: ComfyUI never answered; powering it off")
                self._retire(uuid, "not reachable", force=True)
                return
            if self._stop.is_set():
                return   # the run ended meanwhile: `stop` powers it off
            took = (self.clock() - t0) / 60 + 1.5   # + H3's first model load on a fresh server
            self.c["startup_minutes"] = round(0.5 * float(self.c.get("startup_minutes", 5)) + 0.5 * took, 1)  # learn S
            self.managed[uuid]["url"] = url
            with self.p.lock:
                self.p.cfg["comfy"]["servers"].append(url)
            self.log(f"{uuid}: in the pool after {self.clock() - t0:.0f}s -> {url}")
            if getattr(self.p, "waker", None):
                self.p.waker.vet_later(uuid, url)   # an old-driver machine is replaced, then powered off
        except Exception as e:  # noqa: BLE001
            self.log(f"scale up failed: {e}")
            self.backoff_until = self.clock() + 60 * float(self.c.get("retry_minutes", 1))
        finally:
            self.starting = False

    def _power_on_stopped(self) -> str | None:
        core = self.c.get("core") or []
        insts = sorted((i for i in self.api.instances() if ours(self.c, i) and i.get("status") == "shutdown"),
                       key=lambda i: (i["uuid"] not in core, core.index(i["uuid"]) if i["uuid"] in core else 0))
        return grab(self.api, [i["uuid"] for i in insts if i["uuid"] not in self.managed], self.c, self.log, stop=self._stop)

    def _create(self) -> str | None:
        n = 1 + sum(1 for i in self.api.instances() if (i.get("name") or "").startswith(self.c["name_prefix"]))
        for spec in self.c.get("gpu_specs", ["5090-p"]):
            try:
                uuid = self.api.create(self.c["application_uuid"], self.c["application_version"], spec,
                                       f"{self.c['name_prefix']}-{n}", self.c.get("data_centers") or None)
                self.log(f"{uuid}: created ({spec}); expected to save ~{self.gain_minutes():.1f} min")
                return uuid
            except AutoDLError as e:
                self.log(f"create {spec} failed: {e}")
        return None

    def _retire(self, uuid: str, why: str, force: bool = False) -> bool:
        """Take the server out of the pool and power it off. Under the lock `pick` takes, so a job handed to the server
        after it was judged idle (a vision check, say) keeps it alive: returns False then."""
        m = self.managed.get(uuid)
        if m and m.get("url"):
            with self.p.lock:
                if not force and self.p._assigned.get(m["url"], 0) > 0:
                    return False
                if m["url"] in self.p.cfg["comfy"]["servers"]:
                    self.p.cfg["comfy"]["servers"].remove(m["url"])
        self.managed.pop(uuid, None)
        if self.dry:
            self.log(f"would power off {uuid} ({why})")
            return True
        if not let_go(uuid):
            self.log(f"{uuid}: out of this run's pool ({why}); another run still uses it")
            return True
        try:
            self.api.power_off(uuid)
            forget(uuid)
            self.log(f"{uuid}: powered off ({why})")
        except AutoDLError as e:
            self.log(f"{uuid}: power off FAILED ({e}); run `sheelshort fleet <project> off`")
        return True

    def log(self, msg: str) -> None:
        from .project import log
        log(f"fleet: {msg}")

    def log_raw(self, msg: str) -> None:
        from .project import log
        log(msg)
