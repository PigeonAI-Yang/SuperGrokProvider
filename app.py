from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")
QUOTA_MARKERS = (
    "usage balance exhausted",
    "insufficient_quota",
    "billing quota",
    "quota exhausted",
    "usage limit",
    "run out of credits",
    "spending-limit",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(os.environ.get("SGR_HOME", Path(base) / "SuperGrokRouter"))


def acquire_singleton_mutex(name: str):
    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法创建单例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    return kernel32, handle


def release_singleton_mutex(mutex) -> None:
    if not mutex:
        return
    kernel32, handle = mutex
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


def integration_configs(provider_url: str, api_key: str) -> dict[str, dict[str, str]]:
    quoted_url = json.dumps(provider_url, ensure_ascii=False)
    quoted_key = json.dumps(api_key, ensure_ascii=False)
    hermes = "\n".join(
        (
            "providers:",
            "  supergrok-router:",
            "    name: SuperGrok Router",
            f"    base_url: {quoted_url}",
            f"    api_key: {quoted_key}",
            "    api_mode: codex_responses",
            "    model: grok-4.5",
            "    models:",
            "      grok-4.5:",
            "        context_length: 500000",
            "agent:",
            "  reasoning_effort: high",
        )
    )
    grok_build = "\n".join(
        (
            "[models]",
            'default_reasoning_effort = "high"',
            "",
            "[model.supergrok-router]",
            'model = "grok-4.5"',
            f"base_url = {quoted_url}",
            'api_backend = "responses"',
            f"api_key = {quoted_key}",
            "supports_reasoning_effort = true",
            'reasoning_effort = "high"',
            "context_window = 500000",
        )
    )
    zcode_content = "\n".join(
        (
            "{",
            '  "provider": {',
            '    "supergrok-router": {',
            '      "name": "SuperGrok Router",',
            '      "kind": "openai",',
            '      "source": "custom",',
            f'      "options": {{"apiKey": {quoted_key}, "baseURL": {quoted_url}, "apiKeyRequired": true}},',
            '      "models": {',
            '        "grok-4.5": {',
            '          "name": "Grok 4.5",',
            '          "limit": {"context": 500000},',
            '          "modalities": {"input": ["text"], "output": ["text"]},',
            '          "reasoning": {',
            '            "enabled": true,',
            '            "levels": ["low", "medium", "high"],',
            '            "defaultLevel": "high",',
            '            "providerOptionsByLevel": {',
            '              "low": {"reasoningEffort": "low"},',
            '              "medium": {"reasoningEffort": "medium"},',
            '              "high": {"reasoningEffort": "high"}',
            "            }",
            "          }",
            "        }",
            "      }",
            "    }",
            "  }",
            "}",
        )
    )
    return {
        "zcode": {
            "label": "Zcode Desktop",
            "filename": r"%USERPROFILE%\.zcode\v2\config.json",
            "note": "将 provider.supergrok-router 合并进现有 JSON；推理图标会显示低 / 中 / 高。",
            "content": zcode_content,
        },
        "hermes": {
            "label": "Hermes",
            "filename": r"%USERPROFILE%\.hermes\config.yaml",
            "note": "分别合并 providers.supergrok-router 与 agent.reasoning_effort，保留现有其他字段。",
            "content": hermes,
        },
        "grok_build": {
            "label": "Grok Build",
            "filename": "Grok Build settings.toml",
            "note": "合并到现有设置；默认 high，仍可由客户端请求覆盖为 low / medium / high。",
            "content": grok_build,
        },
    }


def system_proxy_settings() -> tuple[dict[str, str], dict]:
    if os.name != "nt":
        proxies = getproxies()
        return proxies, {"enabled": bool(proxies), "server": proxies.get("https") or proxies.get("http")}
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            try:
                raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
            except FileNotFoundError:
                raw = ""
    except OSError:
        enabled, raw = False, ""
    if not enabled or not raw:
        return {}, {"enabled": False, "server": None}
    values: dict[str, str] = {}
    if "=" in raw:
        for item in raw.split(";"):
            if "=" not in item:
                continue
            scheme, address = item.split("=", 1)
            if scheme in {"http", "https"} and address:
                values[scheme] = address if "://" in address else "http://" + address
    else:
        address = raw if "://" in raw else "http://" + raw
        values = {"http": address, "https": address}
    return values, {"enabled": bool(values), "server": raw if values else None}


def open_with_system_proxy(request: Request, timeout: int = 600):
    proxies, _ = system_proxy_settings()
    # An explicit ProxyHandler prevents urllib from using inherited proxy env or silently changing policy.
    return build_opener(ProxyHandler(proxies)).open(request, timeout=timeout)


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


class AccountStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.accounts_root = self.root / "accounts"
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "version": 1,
                    "api_key": "sgr_" + secrets.token_urlsafe(32),
                    "active_id": None,
                    "accounts": [],
                },
            )

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        atomic_write_json(self.path, data)

    def snapshot(self) -> dict:
        with self.lock:
            return self._read()

    def public_snapshot(self) -> dict:
        data = self.snapshot()
        return {
            "active_id": data.get("active_id"),
            "accounts": [self._public(account) for account in data["accounts"]],
        }

    @staticmethod
    def _public(account: dict) -> dict:
        allowed = (
            "id",
            "name",
            "email",
            "membership_type",
            "state",
            "enabled",
            "created_at",
            "last_used_at",
            "last_error",
            "retry_after",
            "auth_url",
            "device_code",
            "auth_output",
            "usage_percent",
            "usage_period_start",
            "usage_period_end",
            "usage_checked_at",
            "usage_error",
            "product_usage",
        )
        return {key: account.get(key) for key in allowed}

    def create(self, name: str, membership_type: str = "unknown") -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("账号名称必须为 1-60 个字符")
        membership_type = membership_type.strip().lower()
        if membership_type not in {"lite", "super", "heavy", "unknown"}:
            raise ValueError("会员类型必须是 Lite、Super 或 Heavy")
        with self.lock:
            data = self._read()
            if any(item["name"].casefold() == name.casefold() for item in data["accounts"]):
                raise ValueError("账号名称已存在")
            account_id = uuid.uuid4().hex
            account = {
                "id": account_id,
                "name": name,
                "email": None,
                "membership_type": membership_type,
                "state": "pending",
                "enabled": True,
                "created_at": utc_now(),
                "last_used_at": None,
                "last_error": None,
                "retry_after": None,
                "auth_url": None,
                "device_code": None,
                "auth_output": "",
                "usage_percent": None,
                "usage_period_start": None,
                "usage_period_end": None,
                "usage_checked_at": None,
                "usage_error": None,
                "product_usage": [],
            }
            data["accounts"].append(account)
            if not data.get("active_id"):
                data["active_id"] = account_id
            self._write(data)
            self.account_home(account_id).mkdir(parents=True, exist_ok=True)
            return self._public(account)

    def account_home(self, account_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", account_id):
            raise ValueError("无效账号 ID")
        path = (self.accounts_root / account_id / "grok-home").resolve()
        if self.accounts_root not in path.parents:
            raise ValueError("账号路径越界")
        return path

    def get(self, account_id: str) -> dict | None:
        with self.lock:
            return next((a.copy() for a in self._read()["accounts"] if a["id"] == account_id), None)

    def update(self, account_id: str, **changes) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            account.update(changes)
            self._write(data)
            return self._public(account)

    def select(self, account_id: str) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            if account.get("state") != "ready" or not account.get("enabled", True):
                raise ValueError("只能选择已就绪且启用的账号")
            data["active_id"] = account_id
            self._write(data)
            return self._public(account)

    def mark_used(self, account_id: str) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            data["active_id"] = account_id
            account.update(last_used_at=utc_now(), last_error=None)
            self._write(data)
            return self._public(account)

    def delete(self, account_id: str) -> None:
        with self.lock:
            data = self._read()
            before = len(data["accounts"])
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            if len(data["accounts"]) == before:
                raise KeyError(account_id)
            if data.get("active_id") == account_id:
                ready = next(
                    (a for a in data["accounts"] if a.get("state") == "ready" and a.get("enabled", True)),
                    None,
                )
                data["active_id"] = ready["id"] if ready else None
            self._write(data)
        account_dir = self.account_home(account_id).parent
        if account_dir.exists() and self.accounts_root in account_dir.resolve().parents:
            shutil.rmtree(account_dir)

    def candidates(self) -> list[dict]:
        with self.lock:
            data = self._read()
            now = time.time()
            changed = False
            for account in data["accounts"]:
                retry_after = account.get("retry_after")
                if account.get("state") == "cooldown" and retry_after and retry_after <= now:
                    account.update(state="ready", retry_after=None, last_error=None)
                    changed = True
            if changed:
                self._write(data)
            ready = [a.copy() for a in data["accounts"] if a.get("state") == "ready" and a.get("enabled", True)]
            active_id = data.get("active_id")
            # ponytail: xAI billing timestamps are normalized ISO strings; parse only if that contract changes.
            ready.sort(
                key=lambda a: (
                    a.get("usage_period_end") is None,
                    a.get("usage_period_end") or "",
                    -(a.get("usage_percent") if a.get("usage_percent") is not None else -1),
                    a["id"] != active_id,
                    a["created_at"],
                )
            )
            return ready

    def api_key(self) -> str:
        return self.snapshot()["api_key"]


def read_auth_identity(home: Path) -> tuple[str | None, str | None]:
    path = home / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("官方认证文件为空")
    entry = next((v for v in data.values() if isinstance(v, dict) and v.get("key")), None)
    if not entry:
        raise ValueError("官方认证文件中没有可用令牌")
    return entry.get("email"), entry.get("key")


class AuthorizationManager:
    def __init__(self, store: AccountStore, grok_command: str):
        self.store = store
        self.grok_command = grok_command
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen] = {}

    def start(self, account_id: str) -> None:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(account_id)
        with self.lock:
            running = self.processes.get(account_id)
            if running and running.poll() is None:
                raise ValueError("该账号正在授权")
        home = self.store.account_home(account_id)
        home.mkdir(parents=True, exist_ok=True)
        self.store.update(
            account_id,
            state="authorizing",
            last_error=None,
            auth_url=None,
            device_code=None,
            auth_output="正在启动官方 xAI 设备授权...",
        )
        thread = threading.Thread(target=self._run, args=(account_id, home), daemon=True)
        thread.start()

    def _run(self, account_id: str, home: Path) -> None:
        env = os.environ.copy()
        env["GROK_HOME"] = str(home)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        output: list[str] = []
        try:
            process = subprocess.Popen(
                [self.grok_command, "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=flags,
            )
            with self.lock:
                self.processes[account_id] = process
            assert process.stdout is not None
            for line in process.stdout:
                clean = ANSI_RE.sub("", line).strip()
                if not clean:
                    continue
                output.append(clean)
                joined = "\n".join(output[-12:])[-2000:]
                url = next((part for part in clean.split() if part.startswith("https://")), None)
                code_match = DEVICE_CODE_RE.search(clean)
                changes = {"auth_output": joined}
                if url:
                    changes["auth_url"] = url
                if code_match:
                    changes["device_code"] = code_match.group(0)
                self.store.update(account_id, **changes)
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError("官方授权进程未成功完成")
            email, _ = read_auth_identity(home)
            self.store.update(
                account_id,
                state="ready",
                email=email,
                last_error=None,
                auth_url=None,
                device_code=None,
                auth_output="授权完成",
            )
        except Exception as exc:
            if self.store.get(account_id):
                self.store.update(account_id, state="error", last_error=str(exc), auth_output="")
        finally:
            with self.lock:
                self.processes.pop(account_id, None)

    def cancel(self, account_id: str) -> None:
        with self.lock:
            process = self.processes.get(account_id)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def logout(self, account_id: str) -> None:
        home = self.store.account_home(account_id)
        if not (home / "auth.json").exists():
            return
        env = os.environ.copy()
        env["GROK_HOME"] = str(home)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(
            [self.grok_command, "logout"],
            env=env,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )

    def refresh(self, account_id: str) -> bool:
        home = self.store.account_home(account_id)
        env = os.environ.copy()
        env["GROK_HOME"] = str(home)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            [self.grok_command, "models"],
            env=env,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return result.returncode == 0


class UsageMonitor:
    BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"

    def __init__(
        self,
        store: AccountStore,
        auth: AuthorizationManager,
        client_version: str,
        interval: int = 1800,
        jitter: int = 300,
    ):
        self.store = store
        self.auth = auth
        self.client_version = client_version
        self.interval = interval
        self.jitter = jitter
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="usage-monitor")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        self.refresh_all()
        while not self.stop_event.wait(self.interval + secrets.randbelow(self.jitter + 1)):
            self.refresh_all()

    def refresh_all(self) -> None:
        for account in self.store.snapshot()["accounts"]:
            if account.get("enabled", True) and (self.store.account_home(account["id"]) / "auth.json").exists():
                self.refresh_one(account["id"])

    def refresh_one(self, account_id: str, allow_refresh: bool = True) -> dict:
        account = self.store.get(account_id)
        if not account:
            raise KeyError(account_id)
        try:
            home = self.store.account_home(account_id)
            auth_data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
            entry = next((value for value in auth_data.values() if isinstance(value, dict) and value.get("key")), None)
            if not entry:
                raise ValueError("官方认证文件中没有可用令牌")
            request = Request(
                self.BILLING_URL,
                headers={
                    "Authorization": "Bearer " + entry["key"],
                    "x-userid": entry.get("user_id", ""),
                    "x-grok-client-version": self.client_version,
                    "x-grok-client-identifier": "grok-shell",
                    "User-Agent": "xai-grok-cli",
                    "Accept": "application/json",
                },
            )
            for attempt in range(3):
                try:
                    with open_with_system_proxy(request, timeout=30) as response:
                        payload = json.load(response)
                    break
                except HTTPError as exc:
                    if exc.code == 401 and allow_refresh and self.auth.refresh(account_id):
                        return self.refresh_one(account_id, allow_refresh=False)
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"额度接口返回 {exc.code}: {detail}") from exc
                except (OSError, URLError, HTTPException, json.JSONDecodeError):
                    if attempt == 2:
                        raise
                    delay = 1 + secrets.randbelow(3) if attempt == 0 else 3 + secrets.randbelow(4)
                    time.sleep(delay)
            config = payload.get("config", payload)
            percent = float(config["creditUsagePercent"])
            period = config.get("currentPeriod") or {}
            changes = {
                "usage_percent": percent,
                "usage_period_start": period.get("start") or config.get("billingPeriodStart"),
                "usage_period_end": period.get("end") or config.get("billingPeriodEnd"),
                "usage_checked_at": utc_now(),
                "usage_error": None,
                "product_usage": config.get("productUsage") or [],
            }
            if percent >= 100:
                changes.update(state="exhausted", retry_after=None, last_error="额度已用完")
            elif account.get("state") == "exhausted":
                changes.update(state="ready", retry_after=None, last_error=None)
            return self.store.update(account_id, **changes)
        except Exception as exc:
            return self.store.update(account_id, usage_checked_at=utc_now(), usage_error=str(exc))


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, handler, store, auth, upstream, usage=None):
        super().__init__(address, handler)
        self.store = store
        self.auth = auth
        self.usage = usage
        self.upstream = upstream.rstrip("/")
        # ponytail: serialize upstream selection; use per-account locks only if local throughput matters.
        self.route_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SuperGrokRouter/1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    @property
    def app(self) -> RouterServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _valid_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == self.server.server_port

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, error_type: str = "invalid_request_error") -> None:
        self._json(status, {"error": {"message": message, "type": error_type}})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 20 * 1024 * 1024:
            raise ValueError("请求体超过 20MB")
        return self.rfile.read(length)

    def _read_json(self) -> dict:
        body = self._read_body()
        value = json.loads(body or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON 请求体必须是对象")
        return value

    def _management_guard(self) -> bool:
        if not self._valid_host():
            self._error(421, "Host 不允许")
            return False
        if not self._valid_origin():
            self._error(403, "浏览器来源不允许")
            return False
        return True

    def _provider_guard(self) -> bool:
        if not self._valid_host():
            self._error(421, "Host 不允许")
            return False
        expected = "Bearer " + self.app.store.api_key()
        if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            self._error(401, "本地 Provider API Key 无效", "authentication_error")
            return False
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, {"status": "ok"})
        if path == "/api/accounts":
            if self._management_guard():
                return self._json(200, self.app.store.public_snapshot())
            return
        if path == "/api/config":
            if self._management_guard():
                provider_url = f"http://127.0.0.1:{self.server.server_port}/v1"
                api_key = self.app.store.api_key()
                return self._json(
                    200,
                    {
                        "provider_url": provider_url,
                        "api_key": api_key,
                        "upstream": self.app.upstream,
                        "system_proxy": system_proxy_settings()[1],
                        "integrations": integration_configs(provider_url, api_key),
                    },
                )
            return
        if path.startswith("/v1/"):
            if self._provider_guard():
                return self._proxy(b"")
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            if not self._provider_guard():
                return
            try:
                return self._proxy(self._read_body())
            except ValueError as exc:
                return self._error(413, str(exc))
        if not self._management_guard():
            return
        try:
            body = self._read_json()
            if path == "/api/accounts":
                account = self.app.store.create(
                    str(body.get("name", "")),
                    str(body.get("membership_type", "unknown")),
                )
                self.app.auth.start(account["id"])
                return self._json(202, account)
            match = re.fullmatch(
                r"/api/accounts/([0-9a-f]{32})/(authorize|select|reset|toggle|usage|membership)",
                path,
            )
            if not match:
                return self._error(404, "接口不存在")
            account_id, action = match.groups()
            if action == "authorize":
                self.app.auth.start(account_id)
                return self._json(202, self.app.store.get(account_id) or {})
            if action == "select":
                return self._json(200, self.app.store.select(account_id))
            if action == "reset":
                return self._json(
                    200,
                    self.app.store.update(account_id, state="ready", retry_after=None, last_error=None),
                )
            if action == "usage":
                if not self.app.usage:
                    return self._error(503, "额度监控尚未启动")
                return self._json(200, self.app.usage.refresh_one(account_id))
            if action == "membership":
                membership_type = str(body.get("membership_type", "")).strip().lower()
                if membership_type not in {"lite", "super", "heavy"}:
                    raise ValueError("会员类型必须是 Lite、Super 或 Heavy")
                return self._json(200, self.app.store.update(account_id, membership_type=membership_type))
            account = self.app.store.get(account_id)
            if not account:
                raise KeyError(account_id)
            return self._json(200, self.app.store.update(account_id, enabled=not account.get("enabled", True)))
        except KeyError:
            return self._error(404, "账号不存在")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))

    def do_DELETE(self) -> None:
        if not self._management_guard():
            return
        try:
            self._read_body()
        except ValueError as exc:
            return self._error(413, str(exc))
        match = re.fullmatch(r"/api/accounts/([0-9a-f]{32})", urlsplit(self.path).path)
        if not match:
            return self._error(404, "接口不存在")
        account_id = match.group(1)
        try:
            self.app.auth.cancel(account_id)
            self.app.auth.logout(account_id)
            self.app.store.delete(account_id)
            return self._json(200, {"deleted": True})
        except KeyError:
            return self._error(404, "账号不存在")

    def _upstream_target(self) -> str:
        parsed = urlsplit(self.app.upstream)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("上游必须是 HTTPS 地址")
        local = urlsplit(self.path)
        base_path = parsed.path.rstrip("/")
        path = local.path
        if base_path.endswith("/v1") and path.startswith("/v1"):
            path = base_path + path[3:]
        else:
            path = base_path + path
        return urlunsplit((parsed.scheme, parsed.netloc, path, local.query, ""))

    def _proxy(self, body: bytes) -> None:
        with self.app.route_lock:
            candidates = self.app.store.candidates()
            if not candidates:
                return self._error(503, "没有可用账号，请先完成授权或重置账号状态", "accounts_unavailable")
            last_message = "所有账号均不可用"
            for account in candidates:
                try:
                    status, reason, headers, payload, connection, response = self._attempt(
                        account, body, refresh=True
                    )
                except RuntimeError as exc:
                    return self._error(502, str(exc), "upstream_error")
                if status == 401:
                    self.app.store.update(account["id"], state="error", last_error="官方认证已失效")
                    last_message = "账号认证已失效"
                    if connection:
                        connection.close()
                    continue
                error_body = payload.decode("utf-8", errors="replace").lower()
                quota_denied = status in {402, 429} or (
                    status == 403 and any(marker in error_body for marker in QUOTA_MARKERS)
                )
                if quota_denied:
                    exhausted = status == 402 or any(marker in error_body for marker in QUOTA_MARKERS)
                    self.app.store.update(
                        account["id"],
                        state="exhausted" if exhausted else "cooldown",
                        retry_after=None if exhausted else time.time() + 60,
                        last_error="额度已耗尽" if exhausted else "触发速率限制，60 秒后恢复",
                    )
                    last_message = "账号额度已耗尽" if exhausted else "账号暂时限流"
                    if connection:
                        connection.close()
                    continue
                if status == 403:
                    return self._send_upstream(status, reason, headers, payload, response)
                if status >= 500:
                    return self._send_upstream(status, reason, headers, payload, response)
                self.app.store.mark_used(account["id"])
                return self._send_upstream(status, reason, headers, payload, response)
            return self._error(429, last_message, "accounts_exhausted")

    def _attempt(self, account: dict, body: bytes, refresh: bool):
        try:
            _, token = read_auth_identity(self.app.store.account_home(account["id"]))
            target = self._upstream_target()
            blocked = {"authorization", "host", "connection", "content-length", "transfer-encoding"}
            headers = {k: v for k, v in self.headers.items() if k.lower() not in blocked}
            headers["Authorization"] = "Bearer " + (token or "")
            if body:
                headers["Content-Length"] = str(len(body))
            request = Request(target, data=body or None, headers=headers, method=self.command)
            try:
                response = open_with_system_proxy(request, timeout=600)
            except HTTPError as exc:
                response = exc
            response_headers = list(response.headers.items())
            if response.status in {401, 402, 403, 429}:
                payload = response.read()
                response.close()
                if response.status == 401 and refresh and self.app.auth.refresh(account["id"]):
                    return self._attempt(account, body, refresh=False)
                return response.status, response.reason, response_headers, payload, None, None
            return response.status, response.reason, response_headers, b"", None, response
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"上游连接失败: {exc}") from exc

    def _send_upstream(self, status, reason, headers, payload, upstream_response) -> None:
        self.send_response(status, reason)
        blocked = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
        upstream_length = None
        for key, value in headers:
            if key.lower() in blocked:
                continue
            if key.lower() == "content-length":
                try:
                    upstream_length = max(0, int(value))
                except ValueError:
                    upstream_length = None
                continue
            self.send_header(key, value)
        body_forbidden = self.command == "HEAD" or 100 <= status < 200 or status in {204, 304}
        chunked = not body_forbidden and not payload and upstream_response is not None and upstream_length is None
        if payload:
            self.send_header("Content-Length", str(len(payload)))
        elif upstream_length is not None:
            self.send_header("Content-Length", str(upstream_length))
        elif chunked:
            self.send_header("Transfer-Encoding", "chunked")
        elif not body_forbidden:
            self.send_header("Content-Length", "0")
        self.end_headers()
        try:
            if body_forbidden:
                return
            if payload:
                self.wfile.write(payload)
            elif upstream_response is not None:
                remaining = upstream_length
                while remaining is None or remaining > 0:
                    size = 64 * 1024 if remaining is None else min(64 * 1024, remaining)
                    chunk = upstream_response.read(size)
                    if not chunk:
                        if remaining:
                            raise OSError("上游响应在 Content-Length 完成前断开")
                        break
                    if chunked:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                    if remaining is not None:
                        remaining -= len(chunk)
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        except (OSError, ConnectionError):
            self.close_connection = True
            raise
        finally:
            if upstream_response:
                upstream_response.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local multi-account SuperGrok provider")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--upstream", default=os.environ.get("SGR_UPSTREAM", "https://api.x.ai"))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("安全限制：当前版本只能监听 localhost")
    singleton = acquire_singleton_mutex("Local\\SuperGrokRouter.Backend")
    if singleton is False:
        raise SystemExit("SuperGrok Router 已在运行")
    grok_command = shutil.which("grok")
    if not grok_command:
        release_singleton_mutex(singleton)
        raise SystemExit("未找到官方 Grok Build CLI，请先安装并确保 grok 在 PATH 中")
    store = AccountStore(args.data_dir)
    auth = AuthorizationManager(store, grok_command)
    version_result = subprocess.run(
        [grok_command, "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    version_match = re.search(r"\d+\.\d+\.\d+", version_result.stdout + version_result.stderr)
    client_version = version_match.group(0) if version_match else "0.0.0"
    usage = UsageMonitor(store, auth, client_version)
    server = RouterServer((args.host, args.port), Handler, store, auth, args.upstream, usage=usage)
    print(f"SuperGrok Router: http://{args.host}:{args.port}")
    print(f"Data: {args.data_dir.resolve()}")
    usage.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        usage.stop()
        server.server_close()
        release_singleton_mutex(singleton)


if __name__ == "__main__":
    main()
