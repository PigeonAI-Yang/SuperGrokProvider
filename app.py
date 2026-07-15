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
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlunsplit
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


def default_budget_policy() -> dict:
    return {
        "enabled": True,
        "window_hours": 5.0,
        "limit_percent": 5.0,
        "window_started_at": None,
        "baseline_percent": None,
        "override_until": None,
        "permanent_override": False,
    }


def model_reasoning_capability(model_id: str) -> str:
    if model_id == "grok-4.5":
        return "low / medium / high（默认 high）"
    if model_id == "grok-4.3":
        return "无 / 低 / 中 / 高（默认低）"
    if model_id.startswith("grok-4.20-multi-agent"):
        return "low / medium / high / xhigh（控制 Agent 数）"
    if model_id.endswith("-non-reasoning"):
        return "固定关闭"
    if model_id.endswith("-reasoning"):
        return "固定开启"
    if model_id == "grok-build-0.1":
        return "支持推理，强度未声明"
    if model_id.startswith("grok-imagine-"):
        return "不适用"
    return "未声明"


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


def integration_configs(
    provider_url: str, api_key: str, agent_id: str = "zcode", agent_name: str = "ZCODE"
) -> dict[str, dict[str, str]]:
    provider_key = "supergrok-router" if agent_id == "zcode" else f"supergrok-router-{agent_id[:8]}"
    provider_name = f"SuperGrok Router · {agent_name}"
    quoted_url = json.dumps(provider_url, ensure_ascii=False)
    quoted_key = json.dumps(api_key, ensure_ascii=False)
    quoted_name = json.dumps(provider_name, ensure_ascii=False)
    hermes = "\n".join(
        (
            "providers:",
            f"  {provider_key}:",
            f"    name: {quoted_name}",
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
            f"[model.{provider_key}]",
            'model = "grok-4.5"',
            f"base_url = {quoted_url}",
            'api_backend = "responses"',
            f"api_key = {quoted_key}",
            "supports_reasoning_effort = true",
            'reasoning_effort = "high"',
            "context_window = 500000",
        )
    )
    provider_options_by_level = {
        level: {"openaiCompatible": {"reasoningEffort": level}}
        for level in ("low", "medium", "high")
    }
    zcode_content = json.dumps(
        {
            "provider": {
                provider_key: {
                    "name": provider_name,
                    "kind": "openai-compatible",
                    "source": "custom",
                    "options": {"apiKey": api_key, "baseURL": provider_url, "apiKeyRequired": True},
                    "models": {
                        "grok-4.5": {
                            "name": "Grok 4.5",
                            "limit": {"context": 500000},
                            "modalities": {"input": ["text"], "output": ["text"]},
                        }
                    },
                }
            },
            "modelCatalog": {
                "overrides": {
                    f"{provider_key}/grok-4.5": {
                        "supportsReasoning": True,
                        "reasoning": {
                            "enabled": True,
                            "levels": ["low", "medium", "high"],
                            "defaultLevel": "high",
                            "providerOptionsByLevel": provider_options_by_level,
                        },
                    }
                }
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "zcode": {
            "label": "Zcode Desktop",
            "filename": r"%USERPROFILE%\.zcode\v2\config.json",
            "note": f"同时合并 provider.{provider_key} 与 modelCatalog.overrides；low / medium / high 会映射到上游 reasoning_effort。",
            "content": zcode_content,
        },
        "hermes": {
            "label": "Hermes",
            "filename": r"%USERPROFILE%\.hermes\config.yaml",
            "note": f"分别合并 providers.{provider_key} 与 agent.reasoning_effort，保留现有其他字段。",
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
            atomic_write_json(self.path, self._new_state())
        else:
            original = self._read()
            data = json.loads(json.dumps(original))
            if data.get("version", 1) < 2:
                data = self._migrate_v1(data)
            if data.get("version", 2) < 3:
                data = self._migrate_v2(data)
            if not self._find_agent(data, "codex-mcp"):
                mcp = self._new_agent("MCP", "mcp", "codex-mcp")
                zcode = self._find_agent(data, "zcode")
                source_id = (zcode or {}).get("active_group_id") or "default"
                group = self._new_group("ZCODE 默认组", mcp["id"], source_group_id=source_id)
                mcp["active_group_id"] = group["id"]
                data["agents"].append(mcp)
                data["groups"].append(group)
            for group in data.get("groups", []):
                group.pop("account_refs", None)
            for account in data.get("accounts", []):
                if not isinstance(account.get("budget_policy"), dict):
                    account["budget_policy"] = default_budget_policy()
            data["version"] = 4
            if data != original:
                self._write(data)

    @staticmethod
    def _new_agent(
        name: str, kind: str, agent_id: str | None = None, api_key: str | None = None
    ) -> dict:
        agent = {
            "id": agent_id or uuid.uuid4().hex,
            "name": name,
            "kind": kind,
            "api_key": api_key or "sgr_" + secrets.token_urlsafe(32),
            "active_group_id": None,
            "created_at": utc_now(),
        }
        return agent

    @staticmethod
    def _new_group(
        name: str,
        agent_id: str,
        group_id: str | None = None,
        position: int = 0,
        source_group_id: str | None = None,
    ) -> dict:
        return {
            "id": group_id or uuid.uuid4().hex,
            "agent_id": agent_id,
            "name": name,
            "enabled": True,
            "position": position,
            "active_id": None,
            "source_group_id": source_group_id,
            "created_at": utc_now(),
        }

    @classmethod
    def _new_state(cls) -> dict:
        agents = [
            cls._new_agent("ZCODE", "zcode", "zcode"),
            cls._new_agent("GROK BUILD", "grok_build", "grok-build"),
            cls._new_agent("HERMES", "hermes", "hermes"),
            cls._new_agent("MCP", "mcp", "codex-mcp"),
        ]
        groups = [
            cls._new_group("默认组", agent["id"], "default" if agent["id"] == "zcode" else None)
            for agent in agents
        ]
        for agent, group in zip(agents, groups):
            agent["active_group_id"] = group["id"]
        mcp_group = next(group for group in groups if group["agent_id"] == "codex-mcp")
        mcp_group.update(name="ZCODE 默认组", source_group_id="default")
        return {"version": 4, "agents": agents, "groups": groups, "accounts": []}

    @classmethod
    def _migrate_v1(cls, data: dict) -> dict:
        group = {
            "id": "default",
            "name": "默认组",
            "api_key": data.get("api_key") or "sgr_" + secrets.token_urlsafe(32),
            "active_id": data.get("active_id"),
            "created_at": utc_now(),
        }
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        for account in accounts:
            account["group_id"] = "default"
        return {"version": 2, "groups": [group], "accounts": accounts}

    @classmethod
    def _migrate_v2(cls, data: dict) -> dict:
        old_groups = data.get("groups") if isinstance(data.get("groups"), list) else []
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        agents: list[dict] = []
        groups: list[dict] = []
        default = next((group for group in old_groups if group.get("id") == "default"), None)
        zcode = cls._new_agent(
            "ZCODE", "zcode", "zcode", (default or {}).get("api_key")
        )
        if default:
            migrated = {key: value for key, value in default.items() if key != "api_key"}
            migrated.update(agent_id="zcode", enabled=True, position=0)
        else:
            migrated = cls._new_group("默认组", "zcode", "default")
        zcode["active_group_id"] = migrated["id"]
        agents.append(zcode)
        groups.append(migrated)

        for old in old_groups:
            if old.get("id") == "default":
                continue
            agent = cls._new_agent(old.get("name") or "自定义", "custom", api_key=old.get("api_key"))
            migrated = {key: value for key, value in old.items() if key != "api_key"}
            migrated.update(agent_id=agent["id"], enabled=True, position=0)
            agent["active_group_id"] = migrated["id"]
            agents.append(agent)
            groups.append(migrated)

        for agent_id, name, kind in (
            ("grok-build", "GROK BUILD", "grok_build"),
            ("hermes", "HERMES", "hermes"),
            ("codex-mcp", "MCP", "mcp"),
        ):
            agent = cls._new_agent(name, kind, agent_id)
            group = cls._new_group(
                "ZCODE 默认组" if agent_id == "codex-mcp" else "默认组",
                agent_id,
                source_group_id="default" if agent_id == "codex-mcp" else None,
            )
            agent["active_group_id"] = group["id"]
            agents.append(agent)
            groups.append(group)
        return {"version": 4, "agents": agents, "groups": groups, "accounts": accounts}

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        atomic_write_json(self.path, data)

    def snapshot(self) -> dict:
        with self.lock:
            return self._read()

    def public_snapshot(self, agent_id: str = "zcode", group_id: str | None = None) -> dict:
        data = self.snapshot()
        agent = self._find_agent(data, agent_id)
        if not agent:
            raise KeyError(agent_id)
        agent_groups = sorted(
            (group for group in data["groups"] if group.get("agent_id") == agent_id),
            key=lambda group: group.get("position", 0),
        )
        selected_id = group_id or agent.get("active_group_id")
        group = next((item for item in agent_groups if item["id"] == selected_id), None)
        if not group and not group_id and agent_groups:
            group = agent_groups[0]
            selected_id = group["id"]
        if not group:
            raise KeyError(group_id or agent_id)
        public_groups = [self._public_group(item, data["accounts"], len(agent_groups)) for item in agent_groups]
        agent_names = {item["id"]: item["name"] for item in data["agents"]}
        return {
            "active_id": group.get("active_id"),
            "selected_agent_id": agent_id,
            "selected_group_id": selected_id,
            "agents": [self._public_agent(item, data["groups"], data["accounts"]) for item in data["agents"]],
            "groups": public_groups,
            "move_targets": [
                {
                    **self._public_group(item, data["accounts"], 0),
                    "agent_name": agent_names.get(item.get("agent_id"), "未知 Agent"),
                }
                for item in data["groups"] if not item.get("source_group_id")
            ],
            "accounts": [
                {**self._public(account), "shared": bool(group.get("source_group_id"))}
                for account in data["accounts"]
                if account.get("group_id") == (group.get("source_group_id") or selected_id)
            ],
            "reusable_groups": [
                self._public_group(item, data["accounts"], 0)
                for item in data["groups"] if item.get("agent_id") == "zcode"
            ],
        }

    @staticmethod
    def _find_agent(data: dict, agent_id: str) -> dict | None:
        return next((agent for agent in data.get("agents", []) if agent.get("id") == agent_id), None)

    @staticmethod
    def _find_group(data: dict, group_id: str) -> dict | None:
        return next((group for group in data.get("groups", []) if group.get("id") == group_id), None)

    @staticmethod
    def _public_group(group: dict, accounts: list[dict], sibling_count: int = 0) -> dict:
        member_group_id = group.get("source_group_id") or group["id"]
        members = [account for account in accounts if account.get("group_id") == member_group_id]
        return {
            "id": group["id"],
            "name": group["name"],
            "agent_id": group.get("agent_id"),
            "enabled": group.get("enabled", True),
            "position": group.get("position", 0),
            "active_id": group.get("active_id"),
            "source_group_id": group.get("source_group_id"),
            "created_at": group.get("created_at"),
            "account_count": len(members),
            "ready_count": sum(
                1 for account in members if account.get("state") == "ready" and account.get("enabled", True)
            ),
            "is_last": sibling_count == 1,
        }

    @staticmethod
    def _public_agent(agent: dict, groups: list[dict], accounts: list[dict]) -> dict:
        children = [group for group in groups if group.get("agent_id") == agent["id"]]
        child_ids = {group.get("source_group_id") or group["id"] for group in children}
        members = [account for account in accounts if account.get("group_id") in child_ids]
        return {
            "id": agent["id"],
            "name": agent["name"],
            "kind": agent.get("kind", "custom"),
            "active_group_id": agent.get("active_group_id"),
            "created_at": agent.get("created_at"),
            "group_count": len(children),
            "account_count": len(members),
            "ready_count": sum(
                1 for account in members if account.get("state") == "ready" and account.get("enabled", True)
            ),
            "is_preset": agent["id"] in {"zcode", "grok-build", "hermes", "codex-mcp"},
            "budget_alert": agent.get("budget_alert"),
        }

    @staticmethod
    def _public_budget(policy: dict | None) -> dict | None:
        if not policy:
            return None
        return {key: policy.get(key) for key in (
            "enabled", "window_hours", "limit_percent", "window_started_at",
            "override_until", "permanent_override", "alert",
        )}

    def agents(self) -> list[dict]:
        data = self.snapshot()
        return [self._public_agent(agent, data["groups"], data["accounts"]) for agent in data["agents"]]

    def group_config(self, group_id: str) -> dict:
        data = self.snapshot()
        group = self._find_group(data, group_id)
        if not group:
            raise KeyError(group_id)
        return group.copy()

    def agent_config(self, agent_id: str) -> dict:
        data = self.snapshot()
        agent = self._find_agent(data, agent_id)
        if not agent:
            raise KeyError(agent_id)
        return agent.copy()

    def agent_for_key(self, api_key: str) -> dict | None:
        data = self.snapshot()
        for agent in data["agents"]:
            if secrets.compare_digest(api_key, agent["api_key"]):
                return agent.copy()
        return None

    def update_budget_policy(self, account_id: str, enabled, window_hours, limit_percent) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        try:
            hours = float(window_hours)
            percent = float(limit_percent)
        except (TypeError, ValueError) as exc:
            raise ValueError("周期和额度必须是数字") from exc
        if not 0.5 <= hours <= 168:
            raise ValueError("周期必须在 0.5–168 小时之间")
        if not 0.1 <= percent <= 100:
            raise ValueError("额度必须在 0.1%–100% 之间")
        with self.lock:
            data = self._read()
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            policy = account.setdefault("budget_policy", default_budget_policy())
            policy.update(
                enabled=enabled,
                window_hours=hours,
                limit_percent=percent,
                window_started_at=None,
                baseline_percent=None,
                override_until=None,
                permanent_override=False,
                alert=None,
            )
            self._write(data)
            return self._public_budget(policy) or {}

    def budget_allows(self, account_id: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self.lock:
            data = self._read()
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            policy = account.setdefault("budget_policy", default_budget_policy())
            if not policy.get("enabled", True) or policy.get("permanent_override"):
                return True
            override_until = policy.get("override_until")
            if override_until and datetime.fromisoformat(override_until) > now:
                return True
            if account.get("usage_percent") is None:
                return False
            started = datetime.fromisoformat(policy["window_started_at"]) if policy.get("window_started_at") else None
            window = timedelta(hours=float(policy.get("window_hours", 5)))
            if not started or now >= started + window:
                policy.update(
                    window_started_at=now.isoformat(),
                    baseline_percent=float(account["usage_percent"]),
                    override_until=None,
                )
            current = float(account["usage_percent"])
            baseline = float(policy.get("baseline_percent", current))
            if current < baseline:  # Official weekly reset occurred inside our window.
                policy["baseline_percent"] = current
                baseline = current
            allowed = current - baseline < float(policy.get("limit_percent", 5))
            self._write(data)
            return allowed

    def raise_budget_alert(self, agent_id: str, account_ids: list[str]) -> dict:
        with self.lock:
            data = self._read()
            agent = self._find_agent(data, agent_id)
            if not agent:
                raise KeyError(agent_id)
            alert = agent.get("budget_alert")
            if not alert:
                alert = {
                    "id": uuid.uuid4().hex,
                    "created_at": utc_now(),
                    "acknowledged": False,
                    "message": "全部账号已达到当前周期额度上限，Grok MCP 已暂停。",
                    "accounts": [
                        {"id": item["id"], "name": item["name"]}
                        for item in data["accounts"] if item["id"] in account_ids
                    ],
                }
                agent["budget_alert"] = alert
                self._write(data)
            return alert.copy()

    def clear_budget_alert(self, agent_id: str) -> None:
        with self.lock:
            data = self._read()
            agent = self._find_agent(data, agent_id)
            if agent and agent.get("budget_alert"):
                agent["budget_alert"] = None
                self._write(data)

    def authorize_budget(self, account_id: str, mode: str) -> dict:
        if mode not in {"keep", "window", "permanent"}:
            raise ValueError("授权模式无效")
        with self.lock:
            data = self._read()
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            policy = account.setdefault("budget_policy", default_budget_policy())
            if mode == "keep":
                pass
            elif mode == "window":
                started = datetime.fromisoformat(policy["window_started_at"]) if policy.get("window_started_at") else datetime.now(timezone.utc)
                policy["override_until"] = (started + timedelta(hours=float(policy.get("window_hours", 5)))).isoformat()
            else:
                policy["permanent_override"] = True
            mcp = self._find_agent(data, "codex-mcp")
            if mcp:
                if mode == "keep" and mcp.get("budget_alert"):
                    mcp["budget_alert"]["acknowledged"] = True
                else:
                    mcp["budget_alert"] = None
            self._write(data)
            return self._public_budget(policy) or {}

    def create_agent(self, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("Agent 名称必须为 1-40 个字符")
        with self.lock:
            data = self._read()
            if any(agent["name"].casefold() == name.casefold() for agent in data["agents"]):
                raise ValueError("Agent 名称已存在")
            agent = self._new_agent(name, "custom")
            group = self._new_group("默认组", agent["id"])
            agent["active_group_id"] = group["id"]
            data["agents"].append(agent)
            data["groups"].append(group)
            self._write(data)
            result = self._public_agent(agent, data["groups"], data["accounts"])
            result["group_id"] = group["id"]
            return result

    def rename_agent(self, agent_id: str, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("Agent 名称必须为 1-40 个字符")
        with self.lock:
            data = self._read()
            agent = self._find_agent(data, agent_id)
            if not agent:
                raise KeyError(agent_id)
            if agent_id in {"zcode", "grok-build", "hermes"}:
                raise ValueError("预置 Agent 不能重命名")
            if any(item["id"] != agent_id and item["name"].casefold() == name.casefold() for item in data["agents"]):
                raise ValueError("Agent 名称已存在")
            agent["name"] = name
            self._write(data)
            return self._public_agent(agent, data["groups"], data["accounts"])

    def delete_agent(self, agent_id: str) -> None:
        if agent_id in {"zcode", "grok-build", "hermes"}:
            raise ValueError("预置 Agent 不能删除")
        with self.lock:
            data = self._read()
            if not self._find_agent(data, agent_id):
                raise KeyError(agent_id)
            group_ids = {group["id"] for group in data["groups"] if group.get("agent_id") == agent_id}
            if any(account.get("group_id") in group_ids for account in data["accounts"]):
                raise ValueError("请先移走该 Agent 下的账号")
            data["agents"] = [agent for agent in data["agents"] if agent["id"] != agent_id]
            data["groups"] = [group for group in data["groups"] if group.get("agent_id") != agent_id]
            self._write(data)

    def create_group(self, name: str, agent_id: str = "zcode") -> dict:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("分组名称必须为 1-40 个字符")
        with self.lock:
            data = self._read()
            if not self._find_agent(data, agent_id):
                raise ValueError("Agent 不存在")
            siblings = [group for group in data["groups"] if group.get("agent_id") == agent_id]
            if any(group["name"].casefold() == name.casefold() for group in siblings):
                raise ValueError("分组名称已存在")
            group = self._new_group(name, agent_id, position=len(siblings))
            data["groups"].append(group)
            self._write(data)
            return self._public_group(group, data["accounts"], len(siblings) + 1)

    def reuse_group(self, source_group_id: str) -> dict:
        with self.lock:
            data = self._read()
            source = self._find_group(data, source_group_id)
            mcp = self._find_agent(data, "codex-mcp")
            if not source or source.get("agent_id") != "zcode" or not mcp:
                raise KeyError(source_group_id)
            siblings = [item for item in data["groups"] if item.get("agent_id") == mcp["id"]]
            if any(item.get("source_group_id") == source_group_id for item in siblings):
                raise ValueError("该 ZCODE 账号组已复用")
            group = self._new_group(source["name"], mcp["id"], position=len(siblings), source_group_id=source_group_id)
            data["groups"].append(group)
            self._write(data)
            return self._public_group(group, data["accounts"], len(siblings) + 1)

    def rename_group(self, group_id: str, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("分组名称必须为 1-40 个字符")
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise KeyError(group_id)
            if any(
                item["id"] != group_id
                and item.get("agent_id") == group.get("agent_id")
                and item["name"].casefold() == name.casefold()
                for item in data["groups"]
            ):
                raise ValueError("分组名称已存在")
            group["name"] = name
            self._write(data)
            siblings = [item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")]
            return self._public_group(group, data["accounts"], len(siblings))

    def toggle_group(self, group_id: str) -> dict:
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise KeyError(group_id)
            group["enabled"] = not group.get("enabled", True)
            self._write(data)
            siblings = [item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")]
            return self._public_group(group, data["accounts"], len(siblings))

    def reorder_group(self, group_id: str, direction: str) -> dict:
        if direction not in {"up", "down"}:
            raise ValueError("排序方向无效")
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise KeyError(group_id)
            siblings = sorted(
                (item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")),
                key=lambda item: item.get("position", 0),
            )
            index = next(i for i, item in enumerate(siblings) if item["id"] == group_id)
            target_index = index - 1 if direction == "up" else index + 1
            if 0 <= target_index < len(siblings):
                other = siblings[target_index]
                group["position"], other["position"] = other.get("position", 0), group.get("position", 0)
                self._write(data)
            return self._public_group(group, data["accounts"], len(siblings))

    def delete_group(self, group_id: str) -> None:
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise KeyError(group_id)
            if any(account.get("group_id") == group_id for account in data["accounts"]):
                raise ValueError("请先移走该组内的账号")
            if any(item.get("source_group_id") == group_id for item in data["groups"]):
                raise ValueError("请先从 MCP 分页移除该复用组")
            siblings = [item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")]
            if len(siblings) == 1:
                raise ValueError("每个 Agent 至少保留一个账号组")
            data["groups"] = [group for group in data["groups"] if group["id"] != group_id]
            for position, item in enumerate(
                sorted(
                    (item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")),
                    key=lambda item: item.get("position", 0),
                )
            ):
                item["position"] = position
            agent = self._find_agent(data, group.get("agent_id"))
            if agent and agent.get("active_group_id") == group_id:
                replacement = next(
                    (item for item in data["groups"] if item.get("agent_id") == group.get("agent_id")), None
                )
                agent["active_group_id"] = replacement["id"] if replacement else None
            self._write(data)

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
            "usage_inferred",
            "usage_period_start",
            "usage_period_end",
            "usage_checked_at",
            "usage_error",
            "product_usage",
            "group_id",
            "budget_policy",
        )
        result = {key: account.get(key) for key in allowed}
        result["budget_policy"] = AccountStore._public_budget(account.get("budget_policy"))
        return result

    def create(self, name: str, membership_type: str = "unknown", group_id: str = "default") -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("账号名称必须为 1-60 个字符")
        membership_type = membership_type.strip().lower()
        if membership_type not in {"lite", "super", "heavy", "unknown"}:
            raise ValueError("会员类型必须是 Lite、Super 或 Heavy")
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise ValueError("分组不存在")
            if group.get("source_group_id"):
                raise ValueError("MCP 分页只能复用 ZCODE 账号组")
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
                "usage_inferred": False,
                "usage_period_start": None,
                "usage_period_end": None,
                "usage_checked_at": None,
                "usage_error": None,
                "product_usage": [],
                "budget_policy": default_budget_policy(),
                "group_id": group_id,
            }
            data["accounts"].append(account)
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

    def rename_account(self, account_id: str, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 60:
            raise ValueError("账号名称必须为 1-60 个字符")
        with self.lock:
            data = self._read()
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            if any(
                item["id"] != account_id and item["name"].casefold() == name.casefold()
                for item in data["accounts"]
            ):
                raise ValueError("账号名称已存在")
            account["name"] = name
            self._write(data)
            return self._public(account)

    def select(self, account_id: str, group_id: str | None = None) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            if account.get("state") != "ready" or not account.get("enabled", True):
                raise ValueError("只能选择已就绪且启用的账号")
            target_group_id = group_id or account.get("group_id", "default")
            group = self._find_group(data, target_group_id)
            if not group:
                raise KeyError(target_group_id)
            if account.get("group_id", "default") != (group.get("source_group_id") or target_group_id):
                raise ValueError("账号不属于该分组")
            group["active_id"] = account_id
            agent = self._find_agent(data, group.get("agent_id"))
            if agent:
                agent["active_group_id"] = group["id"]
            self._write(data)
            return self._public(account)

    def mark_used(self, account_id: str, group_id: str | None = None) -> dict:
        with self.lock:
            data = self._read()
            account = next((a for a in data["accounts"] if a["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            target_group_id = group_id or account.get("group_id", "default")
            group = self._find_group(data, target_group_id)
            if not group:
                raise KeyError(target_group_id)
            if account.get("group_id", "default") != (group.get("source_group_id") or target_group_id):
                raise ValueError("账号不属于该分组")
            group["active_id"] = account_id
            agent = self._find_agent(data, group.get("agent_id"))
            if agent:
                agent["active_group_id"] = group["id"]
            account.update(last_used_at=utc_now(), last_error=None)
            self._write(data)
            return self._public(account)

    def move(self, account_id: str, group_id: str) -> dict:
        with self.lock:
            data = self._read()
            target = self._find_group(data, group_id)
            if not target:
                raise ValueError("目标分组不存在")
            if target.get("source_group_id"):
                raise ValueError("账号不能移动到 MCP 复用组")
            account = next((item for item in data["accounts"] if item["id"] == account_id), None)
            if not account:
                raise KeyError(account_id)
            source_id = account.get("group_id", "default")
            if source_id == group_id:
                return self._public(account)
            source = self._find_group(data, source_id)
            account["group_id"] = group_id
            if source and source.get("active_id") == account_id:
                replacement = next(
                    (
                        item
                        for item in data["accounts"]
                        if item.get("group_id") == source_id
                        and item.get("state") == "ready"
                        and item.get("enabled", True)
                    ),
                    None,
                )
                source["active_id"] = replacement["id"] if replacement else None
            if not target.get("active_id") and account.get("state") == "ready" and account.get("enabled", True):
                target["active_id"] = account_id
            self._write(data)
            return self._public(account)

    def delete(self, account_id: str) -> None:
        with self.lock:
            data = self._read()
            deleted = next((a for a in data["accounts"] if a["id"] == account_id), None)
            before = len(data["accounts"])
            data["accounts"] = [a for a in data["accounts"] if a["id"] != account_id]
            if len(data["accounts"]) == before:
                raise KeyError(account_id)
            group = self._find_group(data, deleted.get("group_id", "default")) if deleted else None
            if group and group.get("active_id") == account_id:
                ready = next(
                    (
                        a
                        for a in data["accounts"]
                        if a.get("group_id", "default") == group["id"]
                        and a.get("state") == "ready"
                        and a.get("enabled", True)
                    ),
                    None,
                )
                group["active_id"] = ready["id"] if ready else None
            self._write(data)
        account_dir = self.account_home(account_id).parent
        if account_dir.exists() and self.accounts_root in account_dir.resolve().parents:
            shutil.rmtree(account_dir)

    def candidates(self, group_id: str = "default") -> list[dict]:
        with self.lock:
            data = self._read()
            group = self._find_group(data, group_id)
            if not group:
                raise KeyError(group_id)
            now = time.time()
            changed = False
            for account in data["accounts"]:
                retry_after = account.get("retry_after")
                if account.get("state") == "cooldown" and retry_after and retry_after <= now:
                    account.update(state="ready", retry_after=None, last_error=None)
                    changed = True
            if changed:
                self._write(data)
            member_group_id = group.get("source_group_id") or group_id
            ready = [
                a.copy()
                for a in data["accounts"]
                if a.get("group_id", "default") == member_group_id
                and a.get("state") == "ready"
                and a.get("enabled", True)
            ]
            active_id = group.get("active_id")
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

    def routing_groups(self, agent_id: str) -> list[dict]:
        data = self.snapshot()
        if not self._find_agent(data, agent_id):
            raise KeyError(agent_id)
        return sorted(
            (
                group.copy()
                for group in data["groups"]
                if group.get("agent_id") == agent_id and group.get("enabled", True)
            ),
            key=lambda group: group.get("position", 0),
        )

    def api_key(self) -> str:
        return self.agent_config("zcode")["api_key"]


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
            period = config.get("currentPeriod") or {}
            raw_percent = config.get("creditUsagePercent")
            inferred = raw_percent is None
            if raw_percent is None:
                if config.get("productUsage") or not (period.get("start") and period.get("end")):
                    raise ValueError("官方额度响应缺少 creditUsagePercent")
                # ponytail: xAI omits zero-valued usage fields at the start of a valid new period.
                raw_percent = 0
            percent = float(raw_percent)
            changes = {
                "usage_percent": percent,
                "usage_inferred": inferred,
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
        self.lock_registry_guard = threading.Lock()
        self.route_locks: dict[str, threading.Lock] = {}
        self.account_locks: dict[str, threading.Lock] = {}

    def route_lock(self, agent_id: str) -> threading.Lock:
        with self.lock_registry_guard:
            return self.route_locks.setdefault(agent_id, threading.Lock())

    def account_lock(self, account_id: str) -> threading.Lock:
        # ponytail: stale lock entries are tiny; prune only if account churn becomes measurable.
        with self.lock_registry_guard:
            return self.account_locks.setdefault(account_id, threading.Lock())


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

    def _provider_agent(self) -> str | None:
        if not self._valid_host():
            self._error(421, "Host 不允许")
            return None
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            self._error(401, "本地 Provider API Key 无效", "authentication_error")
            return None
        agent = self.app.store.agent_for_key(authorization[7:])
        if not agent:
            self._error(401, "本地 Provider API Key 无效", "authentication_error")
            return None
        return agent["id"]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, {"status": "ok"})
        if path == "/api/accounts":
            if self._management_guard():
                query = parse_qs(urlsplit(self.path).query)
                agent_id = query.get("agent_id", ["zcode"])[0]
                group_id = query.get("group_id", [None])[0]
                try:
                    return self._json(200, self.app.store.public_snapshot(agent_id, group_id))
                except KeyError:
                    return self._error(404, "Agent 或分组不存在")
            return
        account_models_match = re.fullmatch(r"/api/accounts/([0-9a-f]{32})/models", path)
        if account_models_match:
            if not self._management_guard():
                return
            account_id = account_models_match.group(1)
            try:
                return self._json(200, self._account_models(account_id))
            except KeyError:
                return self._error(404, "账号不存在")
            except RuntimeError as exc:
                return self._error(502, str(exc), "upstream_error")
        if path == "/api/agents":
            if self._management_guard():
                return self._json(200, {"agents": self.app.store.agents()})
            return
        if path == "/api/config":
            if self._management_guard():
                agent_id = parse_qs(urlsplit(self.path).query).get("agent_id", ["zcode"])[0]
                try:
                    agent = self.app.store.agent_config(agent_id)
                except KeyError:
                    return self._error(404, "Agent 不存在")
                provider_url = f"http://127.0.0.1:{self.server.server_port}/v1"
                api_key = agent["api_key"]
                return self._json(
                    200,
                    {
                        "provider_url": provider_url,
                        "api_key": api_key,
                        "upstream": self.app.upstream,
                        "system_proxy": system_proxy_settings()[1],
                        "integrations": integration_configs(
                            provider_url, api_key, agent["id"], agent["name"]
                        ),
                        "agent": {
                            "id": agent["id"],
                            "name": agent["name"],
                            "kind": agent.get("kind", "custom"),
                        },
                    },
                )
            return
        if path.startswith("/v1/"):
            agent_id = self._provider_agent()
            if agent_id:
                return self._proxy(b"", agent_id)
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            agent_id = self._provider_agent()
            if not agent_id:
                return
            try:
                return self._proxy(self._read_body(), agent_id)
            except ValueError as exc:
                return self._error(413, str(exc))
        if not self._management_guard():
            return
        try:
            body = self._read_json()
            if path == "/api/agents":
                return self._json(201, self.app.store.create_agent(str(body.get("name", ""))))
            if path == "/api/groups":
                if body.get("source_group_id"):
                    return self._json(201, self.app.store.reuse_group(str(body["source_group_id"])))
                return self._json(
                    201,
                    self.app.store.create_group(
                        str(body.get("name", "")), str(body.get("agent_id", "zcode"))
                    ),
                )
            budget_match = re.fullmatch(r"/api/accounts/([0-9a-f]{32})/budget(?:/(authorize))?", path)
            if budget_match:
                account_id, action = budget_match.groups()
                if action == "authorize":
                    return self._json(200, self.app.store.authorize_budget(account_id, str(body.get("mode", ""))))
                return self._json(
                    200,
                    self.app.store.update_budget_policy(
                        account_id,
                        body.get("enabled"),
                        body.get("window_hours"),
                        body.get("limit_percent"),
                    ),
                )
            if path == "/api/accounts":
                account = self.app.store.create(
                    str(body.get("name", "")),
                    str(body.get("membership_type", "unknown")),
                    str(body.get("group_id", "default")),
                )
                self.app.auth.start(account["id"])
                return self._json(202, account)
            match = re.fullmatch(
                r"/api/accounts/([0-9a-f]{32})/(authorize|select|reset|toggle|usage|membership|move|rename)",
                path,
            )
            agent_match = re.fullmatch(r"/api/agents/([0-9a-z-]{1,64})/rename", path)
            if agent_match:
                return self._json(
                    200,
                    self.app.store.rename_agent(agent_match.group(1), str(body.get("name", ""))),
                )
            group_match = re.fullmatch(
                r"/api/groups/([0-9a-z-]{1,64})/(rename|toggle|reorder)", path
            )
            if group_match:
                group_id, group_action = group_match.groups()
                if group_action == "rename":
                    return self._json(
                        200, self.app.store.rename_group(group_id, str(body.get("name", "")))
                    )
                if group_action == "toggle":
                    return self._json(200, self.app.store.toggle_group(group_id))
                return self._json(
                    200, self.app.store.reorder_group(group_id, str(body.get("direction", "")))
                )
            if not match:
                return self._error(404, "接口不存在")
            account_id, action = match.groups()
            if action == "authorize":
                self.app.auth.start(account_id)
                return self._json(202, self.app.store.get(account_id) or {})
            if action == "select":
                return self._json(200, self.app.store.select(account_id))
            if action == "move":
                with self.app.account_lock(account_id):
                    return self._json(200, self.app.store.move(account_id, str(body.get("group_id", ""))))
            if action == "rename":
                return self._json(200, self.app.store.rename_account(account_id, str(body.get("name", ""))))
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
            return self._error(404, "资源不存在")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, str(exc))

    def do_DELETE(self) -> None:
        if not self._management_guard():
            return
        try:
            self._read_body()
        except ValueError as exc:
            return self._error(413, str(exc))
        path = urlsplit(self.path).path
        agent_match = re.fullmatch(r"/api/agents/([0-9a-z-]{1,64})", path)
        if agent_match:
            try:
                self.app.store.delete_agent(agent_match.group(1))
                return self._json(200, {"deleted": True})
            except KeyError:
                return self._error(404, "Agent 不存在")
            except ValueError as exc:
                return self._error(400, str(exc))
        group_match = re.fullmatch(r"/api/groups/([0-9a-z-]{1,64})", path)
        if group_match:
            try:
                self.app.store.delete_group(group_match.group(1))
                return self._json(200, {"deleted": True})
            except KeyError:
                return self._error(404, "分组不存在")
            except ValueError as exc:
                return self._error(400, str(exc))
        match = re.fullmatch(r"/api/accounts/([0-9a-f]{32})", path)
        if not match:
            return self._error(404, "接口不存在")
        account_id = match.group(1)
        try:
            with self.app.account_lock(account_id):
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

    def _account_models(self, account_id: str) -> dict:
        account = self.app.store.get(account_id)
        if not account:
            raise KeyError(account_id)
        try:
            with self.app.account_lock(account_id):
                return {"models": self._fetch_account_models(account_id), "source": "account"}
        except PermissionError as selected_error:
            group = self.app.store.group_config(account.get("group_id", "default"))
            for fallback_group in self.app.store.routing_groups(group["agent_id"]):
                for fallback in self.app.store.candidates(fallback_group["id"]):
                    if fallback["id"] == account_id:
                        continue
                    try:
                        with self.app.account_lock(fallback["id"]):
                            return {"models": self._fetch_account_models(fallback["id"]), "source": "agent"}
                    except PermissionError:
                        continue
            raise RuntimeError(str(selected_error)) from selected_error

    def _fetch_account_models(self, account_id: str, refresh: bool = True) -> list[dict]:
        try:
            target = self.app.upstream + ("/models" if self.app.upstream.endswith("/v1") else "/v1/models")
            network_failures = 0
            refreshed = False
            while True:
                _, token = read_auth_identity(self.app.store.account_home(account_id))
                request = Request(
                    target,
                    headers={"Authorization": "Bearer " + (token or ""), "Accept": "application/json"},
                )
                try:
                    with open_with_system_proxy(request, timeout=30) as response:
                        payload = json.load(response)
                    break
                except HTTPError as exc:
                    if exc.code == 401 and refresh and not refreshed and self.app.auth.refresh(account_id):
                        refreshed = True
                        continue
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                    if exc.code in {401, 402, 429} or (
                        exc.code == 403 and any(marker in detail.lower() for marker in QUOTA_MARKERS)
                    ):
                        raise PermissionError(f"模型接口返回 {exc.code}: {detail}") from exc
                    raise RuntimeError(f"模型接口返回 {exc.code}: {detail}") from exc
                except (OSError, URLError, HTTPException, json.JSONDecodeError) as exc:
                    network_failures += 1
                    if network_failures == 3:
                        raise RuntimeError(f"模型查询失败: {exc}") from exc
                    time.sleep(network_failures + secrets.randbelow(2))
            models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                raise RuntimeError("模型接口响应格式无效")
            return [
                {
                    "id": model["id"],
                    "reasoning": model_reasoning_capability(model["id"]),
                }
                for model in models
                if isinstance(model, dict) and isinstance(model.get("id"), str)
            ]
        except RuntimeError:
            raise
        except PermissionError:
            raise
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"模型查询失败: {exc}") from exc

    def _proxy(self, body: bytes, agent_id: str) -> None:
        with self.app.route_lock(agent_id):
            groups = self.app.store.routing_groups(agent_id)
            source_ids = {group.get("source_group_id") or group["id"] for group in groups}
            configured_ids = [
                account["id"] for account in self.app.store.snapshot()["accounts"]
                if account.get("group_id") in source_ids and account.get("enabled", True)
            ]
            enforce_budget = (
                self.command == "POST"
                and urlsplit(self.path).path in {"/v1/responses", "/v1/chat/completions"}
            )
            had_candidates = False
            budget_denied = False
            gated_ids: list[str] = []
            last_message = "所有账号均不可用"
            for group in groups:
                candidates = self.app.store.candidates(group["id"])
                had_candidates = had_candidates or bool(candidates)
                for account in candidates:
                    with self.app.account_lock(account["id"]):
                        if enforce_budget:
                            if self.app.usage:
                                account = self.app.usage.refresh_one(account["id"])
                            if account.get("state") != "ready":
                                continue
                            if not self.app.store.budget_allows(account["id"]):
                                budget_denied = True
                                gated_ids.append(account["id"])
                                continue
                            self.app.store.clear_budget_alert("codex-mcp")
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
                        self.app.store.mark_used(account["id"], group["id"])
                        return self._send_upstream(status, reason, headers, payload, response)
            if budget_denied:
                self.app.store.raise_budget_alert("codex-mcp", gated_ids)
                return self._error(
                    429,
                    "全部账号已达到当前周期额度上限；请在 SuperGrok Router 中授权放开。",
                    "budget_gate",
                )
            if not had_candidates:
                if configured_ids:
                    return self._error(429, "所有账号均不可用或官方额度已耗尽", "accounts_exhausted")
                return self._error(503, "该 Agent 没有启用且可用的账号组", "agent_accounts_unavailable")
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


def create_router_server(
    host: str = "127.0.0.1",
    port: int = 8742,
    data_dir: Path | None = None,
    upstream: str | None = None,
) -> tuple[RouterServer, object]:
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("安全限制：当前版本只能监听 localhost")
    singleton = acquire_singleton_mutex("Local\\SuperGrokRouter.Backend")
    if singleton is False:
        raise RuntimeError("SuperGrok Router 已在运行")
    try:
        grok_command = shutil.which("grok")
        if not grok_command:
            raise RuntimeError("未找到官方 Grok Build CLI，请先安装并确保 grok 在 PATH 中")
        store = AccountStore(data_dir or default_data_dir())
        auth = AuthorizationManager(store, grok_command)
        version_result = subprocess.run(
            [grok_command, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        version_match = re.search(r"\d+\.\d+\.\d+", version_result.stdout + version_result.stderr)
        client_version = version_match.group(0) if version_match else "0.0.0"
        usage = UsageMonitor(store, auth, client_version)
        server = RouterServer(
            (host, port), Handler, store, auth, upstream or os.environ.get("SGR_UPSTREAM", "https://api.x.ai"), usage=usage
        )
        return server, singleton
    except Exception:
        release_singleton_mutex(singleton)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Local multi-account SuperGrok provider")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--upstream", default=os.environ.get("SGR_UPSTREAM", "https://api.x.ai"))
    args = parser.parse_args()
    try:
        server, singleton = create_router_server(args.host, args.port, args.data_dir, args.upstream)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"SuperGrok Router: http://{args.host}:{args.port}")
    print(f"Data: {args.data_dir.resolve()}")
    server.usage.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.usage.stop()
        server.server_close()
        release_singleton_mutex(singleton)


if __name__ == "__main__":
    main()
