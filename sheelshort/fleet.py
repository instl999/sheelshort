"""AutoDL.Art app instances as ComfyUI servers: create, power on/off, release, and find their ComfyUI address.

The user's servers are instances of an AutoDL.Art application (ComfyUI with MiniMax H3 and the other models baked
in), so an identical server is one `create` call with the same application and version. API docs:
https://www.autodl.art/docs/app_api/ . The developer token is read from $AUTODL_TOKEN or
~/.config/sheelshort/autodl_token and never written anywhere else.
"""
from __future__ import annotations

import os
import threading
import time
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


def _owned() -> dict:
    import json
    return json.loads(OWNED.read_text()) if OWNED.exists() else {}


def _own(uuid: str, pid: int | None) -> None:
    import json
    d = _owned()
    if pid is None:
        d.pop(uuid, None)
    else:
        d[uuid] = pid
    OWNED.parent.mkdir(parents=True, exist_ok=True)
    OWNED.write_text(json.dumps(d))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reap_orphans(api: "AutoDL", log=print) -> list[str]:
    """Power off servers a dead sheelshort process had started (killed before it could clean up)."""
    done = []
    for uuid, pid in _owned().items():
        if pid == os.getpid() or _alive(pid):
            continue
        try:
            if api.status(uuid) not in ("shutdown",):
                api.power_off(uuid)
                log(f"fleet: powered off orphan {uuid} (its run, pid {pid}, ended without cleaning up)")
            done.append(uuid)
            _own(uuid, None)
        except AutoDLError as e:
            if "NotFound" in str(e) or "未查询到" in str(e):   # released meanwhile: nothing left to do
                _own(uuid, None)
            else:
                log(f"fleet: could not power off orphan {uuid}: {e}")
    return done


class Fleet:
    """Grows and shrinks the render pool with extra AutoDL.Art instances of the same app while a render runs.

    Only instances this engine named (`name_prefix`-…) in the allowed regions are ever touched; the user's own servers
    in `[comfy] servers` stay as they are. Every managed instance is powered off when the run ends (`stop`), and an
    idle one is drained and powered off earlier (`idle_off_minutes`). With `dry_run` the decisions are only logged.
    """

    def __init__(self, p, api: AutoDL | None = None, clock=time.time):
        self.p, self.c = p, p.cfg["fleet"]
        self.dry = bool(self.c.get("dry_run", True))
        self.api = api if api is not None else AutoDL()
        self.clock = clock
        self.pending = 0
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
        if not self.dry:
            self._adopt_orphans()
        self.pending = jobs
        self.log(f"start: {jobs} job(s), {len(self.p.cfg['comfy']['servers'])} server(s), up to {self.c['max_extra']} extra"
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
            self._retire(uuid, "run finished")

    def _adopt_orphans(self) -> None:
        """Servers left running by a run that died are taken into this run's pool (they are ours, and already warm);
        stopped ones are just forgotten. The idle rules power them off if this run has no work for them."""
        for uuid, pid in _owned().items():
            if pid == os.getpid() or _alive(pid):
                continue
            try:
                status = self.api.status(uuid)
            except AutoDLError:
                _own(uuid, None)
                continue
            url = self.api.comfy_url(uuid) if status == "running" else ""
            if not url:
                _own(uuid, None)
                continue
            self.managed[uuid] = {"url": url, "idle_since": None}
            _own(uuid, os.getpid())
            with self.p.lock:
                if url not in self.p.cfg["comfy"]["servers"]:
                    self.p.cfg["comfy"]["servers"].append(url)
            self.log(f"{uuid}: adopted from a run that ended without cleaning up -> {url}")

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
        if (len(self.managed) < int(self.c["max_extra"]) and not self.starting and self.clock() >= self.backoff_until
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
            if not busy and (self.pending == 0 or idle_for >= 60 * float(self.c.get("idle_off_minutes", 10))):
                if self.pending:   # work is waiting but this server got none: another server would sit idle too
                    self.hold_pending = self.pending
                self._retire(uuid, f"idle {idle_for / 60:.0f} min" if self.pending else "no work left")

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
                self.backoff_until = self.clock() + 60 * float(self.c.get("retry_minutes", 5))
                return
            self.managed[uuid] = {"url": "", "idle_since": None}   # owned from here on, so `stop` powers it off
            _own(uuid, os.getpid())                                # and a later run powers it off if this one dies
            t0 = self.clock()
            self.api.wait_status(uuid, "running", timeout_s=900)
            url = ""
            while not url and self.clock() - t0 < 1200 and not self._stop.is_set():
                url = self.api.comfy_url(uuid)
                if not url:
                    time.sleep(10)
            if not url:
                self.log(f"{uuid}: ComfyUI never answered; powering it off")
                self._retire(uuid, "not reachable")
                return
            if self._stop.is_set():
                return   # the run ended meanwhile: `stop` powers it off
            took = (self.clock() - t0) / 60 + 1.5   # + H3's first model load on a fresh server
            self.c["startup_minutes"] = round(0.5 * float(self.c.get("startup_minutes", 5)) + 0.5 * took, 1)  # learn S
            self.managed[uuid]["url"] = url
            with self.p.lock:
                self.p.cfg["comfy"]["servers"].append(url)
            self.log(f"{uuid}: in the pool after {self.clock() - t0:.0f}s -> {url}")
        except Exception as e:  # noqa: BLE001
            self.log(f"scale up failed: {e}")
            self.backoff_until = self.clock() + 60 * float(self.c.get("retry_minutes", 5))
        finally:
            self.starting = False

    def _ours(self, inst: dict) -> bool:
        return (inst.get("name") or "").startswith(self.c["name_prefix"]) and \
            any(r in (inst.get("region_name") or "") for r in self.c.get("region_names", ["北京"]))

    def _power_on_stopped(self) -> str | None:
        for inst in self.api.instances():
            if self._ours(inst) and inst.get("status") == "shutdown" and inst["uuid"] not in self.managed:
                try:
                    self.api.power_on(inst["uuid"])
                    self.log(f"{inst['uuid']}: powered on")
                    return inst["uuid"]
                except AutoDLError as e:
                    self.log(f"{inst['uuid']}: power on failed ({e})")
        return None

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

    def _retire(self, uuid: str, why: str) -> None:
        m = self.managed.pop(uuid, None)
        if m and m.get("url"):
            with self.p.lock:
                if m["url"] in self.p.cfg["comfy"]["servers"]:
                    self.p.cfg["comfy"]["servers"].remove(m["url"])
        if self.dry:
            self.log(f"would power off {uuid} ({why})")
            return
        try:
            self.api.power_off(uuid)
            _own(uuid, None)
            self.log(f"{uuid}: powered off ({why})")
        except AutoDLError as e:
            self.log(f"{uuid}: power off FAILED ({e}); run `sheelshort fleet <project> off`")

    def log(self, msg: str) -> None:
        from .project import log
        log(f"fleet: {msg}")

    def log_raw(self, msg: str) -> None:
        from .project import log
        log(msg)


def managed(api: AutoDL, prefix: str) -> list[dict]:
    return [i for i in api.instances() if (i.get("name") or "").startswith(prefix)]
