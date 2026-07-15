import json
import http.client
import io
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from app import AccountStore, Handler, RouterServer, UsageMonitor, read_auth_identity  # noqa: E402


class FakeAuth:
    def start(self, account_id):
        return None

    def cancel(self, account_id):
        return None

    def logout(self, account_id):
        return None

    def refresh(self, account_id):
        return False


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class IntegrationConfigTests(unittest.TestCase):
    def setUp(self):
        self.url = "http://127.0.0.1:8742/v1"
        self.key = "sgr_contract_key"
        self.configs = app.integration_configs(self.url, self.key)

    def test_zcode_uses_current_reasoning_level_schema(self):
        payload = json.loads(self.configs["zcode"]["content"])
        reasoning = payload["modelCatalog"]["overrides"]["supergrok-router/grok-4.5"]["reasoning"]
        self.assertEqual(payload["provider"]["supergrok-router"]["kind"], "openai-compatible")
        self.assertEqual(reasoning["levels"], ["low", "medium", "high"])
        self.assertEqual(reasoning["defaultLevel"], "high")
        self.assertEqual(
            reasoning["providerOptionsByLevel"]["low"]["openaiCompatible"],
            {"reasoningEffort": "low"},
        )
        self.assertEqual(payload["provider"]["supergrok-router"]["options"]["apiKey"], self.key)

    def test_hermes_and_grok_build_use_responses_reasoning_contracts(self):
        hermes = self.configs["hermes"]["content"]
        self.assertIn("api_mode: codex_responses", hermes)
        self.assertIn("reasoning_effort: high", hermes)
        grok_build = self.configs["grok_build"]["content"]
        self.assertIn('api_backend = "responses"', grok_build)
        self.assertIn("supports_reasoning_effort = true", grok_build)
        self.assertNotIn("grok-composer", hermes + grok_build + self.configs["zcode"]["content"])


class ServerStartupTests(unittest.TestCase):
    def test_server_rejects_non_loopback_binding_before_startup(self):
        with self.assertRaisesRegex(RuntimeError, "localhost"):
            app.create_router_server("0.0.0.0", 8742)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_account_lifecycle_and_public_shape(self):
        account = self.store.create("主账号", "heavy")
        home = self.store.account_home(account["id"])
        self.assertTrue(home.exists())
        ready = self.store.update(account["id"], state="ready", email="owner@example.com")
        self.store.select(account["id"])
        self.assertEqual(ready["email"], "owner@example.com")
        self.assertEqual(ready["membership_type"], "heavy")
        self.assertIsNotNone(ready["created_at"])
        self.assertNotIn("key", ready)
        self.store.delete(account["id"])
        self.assertFalse(home.parent.exists())

    def test_membership_type_is_validated(self):
        with self.assertRaisesRegex(ValueError, "会员类型"):
            self.store.create("错误套餐", "premium")

    def test_v1_state_migrates_to_default_group_without_changing_key_or_active_account(self):
        account_id = "a" * 32
        self.store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "api_key": "sgr_existing_key",
                    "active_id": account_id,
                    "accounts": [{"id": account_id, "name": "旧账号", "state": "ready", "enabled": True}],
                }
            ),
            encoding="utf-8",
        )
        migrated = AccountStore(Path(self.temp.name))
        state = migrated.snapshot()
        self.assertEqual(state["version"], 4)
        self.assertEqual(next(agent for agent in state["agents"] if agent["id"] == "zcode")["api_key"], "sgr_existing_key")
        self.assertEqual(next(group for group in state["groups"] if group["id"] == "default")["active_id"], account_id)
        self.assertEqual(state["accounts"][0]["group_id"], "default")
        self.assertEqual({agent["id"] for agent in state["agents"] if agent["kind"] != "custom"}, {"zcode", "grok-build", "hermes", "codex-mcp"})

    def test_v2_groups_migrate_to_agents_without_losing_keys_or_membership(self):
        custom_group_id = "b" * 32
        self.store.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "groups": [
                        {"id": "default", "name": "默认组", "api_key": "sgr_zcode_old", "active_id": None},
                        {"id": custom_group_id, "name": "旧 Codex", "api_key": "sgr_custom_old", "active_id": None},
                    ],
                    "accounts": [{"id": "c" * 32, "name": "旧成员", "group_id": custom_group_id}],
                }
            ),
            encoding="utf-8",
        )
        migrated = AccountStore(Path(self.temp.name)).snapshot()
        self.assertEqual(migrated["version"], 4)
        self.assertEqual(next(agent for agent in migrated["agents"] if agent["id"] == "zcode")["api_key"], "sgr_zcode_old")
        custom = next(agent for agent in migrated["agents"] if agent["kind"] == "custom")
        self.assertEqual(custom["api_key"], "sgr_custom_old")
        self.assertEqual(next(group for group in migrated["groups"] if group["id"] == custom_group_id)["agent_id"], custom["id"])
        self.assertEqual(migrated["accounts"][0]["group_id"], custom_group_id)

    def test_v3_state_adds_mcp_group_reference_and_per_account_gate(self):
        account = self.store.create("旧 v3 账号")
        state = self.store.snapshot()
        state["version"] = 3
        state["agents"] = [item for item in state["agents"] if item["id"] != "codex-mcp"]
        state["groups"] = [item for item in state["groups"] if item.get("agent_id") != "codex-mcp"]
        state["accounts"][0].pop("budget_policy", None)
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        migrated = AccountStore(Path(self.temp.name)).snapshot()
        self.assertEqual(migrated["version"], 4)
        mcp_group = next(item for item in migrated["groups"] if item.get("agent_id") == "codex-mcp")
        self.assertEqual(mcp_group["source_group_id"], "default")
        self.assertTrue(migrated["accounts"][0]["budget_policy"]["enabled"])

    def test_group_lifecycle_and_exclusive_account_move(self):
        group = self.store.create_group("Zcode")
        account = self.store.create("分组账号", "super", group["id"])
        self.store.update(account["id"], state="ready")
        self.store.select(account["id"])
        self.assertEqual(self.store.group_config(group["id"])["active_id"], account["id"])
        with self.assertRaisesRegex(ValueError, "移走"):
            self.store.delete_group(group["id"])
        moved = self.store.move(account["id"], "default")
        self.assertEqual(moved["group_id"], "default")
        self.assertIsNone(self.store.group_config(group["id"])["active_id"])
        self.store.delete_group(group["id"])
        empty_agent = self.store.create_agent("空 Agent")
        with self.assertRaisesRegex(ValueError, "至少保留"):
            self.store.delete_group(empty_agent["group_id"])

    def test_any_agent_can_reuse_another_agents_account_group(self):
        account = self.store.create("共享账号", "heavy")
        self.store.update(account["id"], state="ready")
        reused = self.store.reuse_group("default", "grok-build")
        snapshot = self.store.public_snapshot("grok-build", reused["id"])
        self.assertEqual([item["id"] for item in snapshot["accounts"]], [account["id"]])
        self.assertTrue(snapshot["accounts"][0]["shared"])
        with self.assertRaisesRegex(ValueError, "同一 Agent"):
            self.store.reuse_group("default", "zcode")

    def test_cooldown_recovers_but_exhausted_does_not(self):
        cool = self.store.create("限流账号")
        spent = self.store.create("耗尽账号")
        self.store.update(cool["id"], state="cooldown", retry_after=time.time() - 1)
        self.store.update(spent["id"], state="exhausted")
        candidates = self.store.candidates()
        self.assertEqual([item["id"] for item in candidates], [cool["id"]])

    def test_candidates_use_earliest_reset_then_highest_usage(self):
        low = self.store.create("同周期低使用率")
        high = self.store.create("同周期高使用率")
        later = self.store.create("较晚重置")
        for account, end, percent in (
            (low, "2026-07-12T00:00:00Z", 20.0),
            (high, "2026-07-12T00:00:00Z", 80.0),
            (later, "2026-07-19T00:00:00Z", 90.0),
        ):
            self.store.update(account["id"], state="ready", usage_period_end=end, usage_percent=percent)
        self.store.select(later["id"])
        self.assertEqual([a["id"] for a in self.store.candidates()], [high["id"], low["id"], later["id"]])

    def test_each_account_has_an_independent_five_hour_five_percent_gate(self):
        account = self.store.create("闸门账号")
        start = app.datetime(2026, 7, 12, tzinfo=app.timezone.utc)
        self.store.update(account["id"], usage_percent=10.0)
        self.assertTrue(self.store.budget_allows(account["id"], start))
        self.store.update(account["id"], usage_percent=15.0)
        self.assertFalse(self.store.budget_allows(account["id"], start + app.timedelta(hours=1)))
        self.assertTrue(self.store.budget_allows(account["id"], start + app.timedelta(hours=5)))
        policy = self.store.get(account["id"])["budget_policy"]
        self.assertEqual((policy["window_hours"], policy["limit_percent"]), (5.0, 5.0))

    def test_auth_reader_does_not_require_fixed_root_key(self):
        account = self.store.create("认证账号")
        auth_path = self.store.account_home(account["id"]) / "auth.json"
        auth_path.write_text(
            json.dumps({"issuer::client": {"email": "a@example.com", "key": "secret-token"}}),
            encoding="utf-8",
        )
        email, token = read_auth_identity(auth_path.parent)
        self.assertEqual(email, "a@example.com")
        self.assertEqual(token, "secret-token")

    def test_upstream_opener_uses_explicit_system_proxy(self):
        opener = Mock()
        with patch.object(app, "system_proxy_settings", return_value=({"https": "http://127.0.0.1:7890"}, {})), patch.object(
            app, "build_opener", return_value=opener
        ) as build:
            app.open_with_system_proxy(urllib.request.Request("https://api.x.ai/v1/models"), timeout=9)
        handler = build.call_args.args[0]
        self.assertEqual(handler.proxies["https"], "http://127.0.0.1:7890")
        opener.open.assert_called_once()

    def test_usage_monitor_exhausts_and_restores_account(self):
        account = self.store.create("额度账号")
        home = self.store.account_home(account["id"])
        (home / "auth.json").write_text(
            json.dumps({"issuer::client": {"key": "token", "user_id": "user-1"}}), encoding="utf-8"
        )
        self.store.update(account["id"], state="ready")
        monitor = UsageMonitor(self.store, FakeAuth(), "0.2.82")
        exhausted = {
            "config": {
                "creditUsagePercent": 100.0,
                "currentPeriod": {"start": "2026-07-05T00:00:00Z", "end": "2026-07-12T00:00:00Z"},
                "productUsage": [{"product": "GrokBuild", "usagePercent": 100.0}],
            }
        }
        with patch.object(app, "open_with_system_proxy", return_value=FakeResponse(exhausted)):
            result = monitor.refresh_one(account["id"])
        self.assertEqual(result["state"], "exhausted")
        self.assertEqual(result["usage_percent"], 100.0)
        self.assertFalse(result["usage_inferred"])

        restored = {"config": {"creditUsagePercent": 12.5, "billingPeriodEnd": "2026-07-19T00:00:00Z"}}
        with patch.object(app, "open_with_system_proxy", return_value=FakeResponse(restored)):
            result = monitor.refresh_one(account["id"])
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["usage_percent"], 12.5)

    def test_usage_monitor_retries_transient_failures_twice(self):
        account = self.store.create("重试账号")
        home = self.store.account_home(account["id"])
        (home / "auth.json").write_text('{"profile":{"key":"token"}}', encoding="utf-8")
        success = FakeResponse({"config": {"creditUsagePercent": 42.0}})
        monitor = UsageMonitor(self.store, FakeAuth(), "0.2.82")
        with patch.object(
            app,
            "open_with_system_proxy",
            side_effect=[urllib.error.URLError("ssl eof"), urllib.error.URLError("proxy reset"), success],
        ) as opener, patch.object(app.secrets, "randbelow", side_effect=[1, 2]), patch.object(
            app.time, "sleep"
        ) as sleep:
            result = monitor.refresh_one(account["id"])
        self.assertEqual(result["usage_percent"], 42.0)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5])

    def test_usage_monitor_marks_valid_empty_period_as_inferred_zero(self):
        account = self.store.create("新周期账号")
        home = self.store.account_home(account["id"])
        (home / "auth.json").write_text('{"profile":{"key":"token"}}', encoding="utf-8")
        self.store.update(account["id"], state="exhausted", usage_percent=100.0, usage_error="old")
        payload = {
            "config": {
                "currentPeriod": {"start": "2026-07-12T13:07:06Z", "end": "2026-07-19T13:07:06Z"},
                "billingPeriodStart": "2026-07-12T13:07:06Z",
                "billingPeriodEnd": "2026-07-19T13:07:06Z",
            }
        }
        with patch.object(app, "open_with_system_proxy", return_value=FakeResponse(payload)):
            result = UsageMonitor(self.store, FakeAuth(), "0.2.82").refresh_one(account["id"])
        self.assertEqual(
            (result["state"], result["usage_percent"], result["usage_inferred"], result["usage_error"]),
            ("ready", 0.0, True, None),
        )

    def test_usage_monitor_does_not_retry_http_4xx(self):
        account = self.store.create("四百错误账号")
        home = self.store.account_home(account["id"])
        (home / "auth.json").write_text('{"profile":{"key":"token"}}', encoding="utf-8")
        error = urllib.error.HTTPError("https://example.invalid", 403, "Forbidden", {}, io.BytesIO(b"denied"))
        monitor = UsageMonitor(self.store, FakeAuth(), "0.2.82")
        with patch.object(app, "open_with_system_proxy", side_effect=error) as opener, patch.object(
            app.time, "sleep"
        ) as sleep:
            result = monitor.refresh_one(account["id"])
        self.assertIn("403", result["usage_error"])
        opener.assert_called_once()
        sleep.assert_not_called()

    def test_usage_monitor_adds_random_delay_after_thirty_minutes(self):
        monitor = UsageMonitor(self.store, FakeAuth(), "0.2.82")
        monitor.refresh_all = Mock()
        delays = []
        monitor.stop_event.wait = Mock(side_effect=lambda delay: delays.append(delay) or True)
        with patch.object(app.secrets, "randbelow", return_value=217) as random_delay:
            monitor._loop()
        self.assertEqual(delays, [2017])
        random_delay.assert_called_once_with(301)
        monitor.refresh_all.assert_called_once_with()

    def test_usage_monitor_still_queues_every_authenticated_account(self):
        ready = self.store.create("可用账号")
        exhausted = self.store.create("耗尽账号")
        for account, state in ((ready, "ready"), (exhausted, "exhausted")):
            home = self.store.account_home(account["id"])
            (home / "auth.json").write_text('{"profile":{"key":"token"}}', encoding="utf-8")
            self.store.update(account["id"], state=state)
        monitor = UsageMonitor(self.store, FakeAuth(), "0.2.82")
        monitor.refresh_one = Mock()
        monitor.refresh_all()
        self.assertEqual([call.args[0] for call in monitor.refresh_one.call_args_list], [ready["id"], exhausted["id"]])


class ProviderRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.temp.name))
        self.first = self.store.create("第一个")
        self.second = self.store.create("第二个")
        self.store.update(self.first["id"], state="ready", usage_percent=0.0)
        self.store.update(self.second["id"], state="ready", usage_percent=0.0)
        self.server = RouterServer(("127.0.0.1", 0), Handler, self.store, FakeAuth(), "https://api.x.ai")
        self.original_attempt = Handler._attempt

        def fake_attempt(handler, account, body, refresh):
            if account["id"] == self.first["id"]:
                return 403, "Forbidden", [], b'{"code":"personal-team-blocked:spending-limit","error":"run out of credits"}', None, None
            payload = b'{"id":"response_ok"}'
            return 200, "OK", [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))], payload, None, None

        Handler._attempt = fake_attempt
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        Handler._attempt = self.original_attempt
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_provider_rotates_after_explicit_exhaustion(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"ping"}',
            headers={
                "Authorization": "Bearer " + self.store.api_key(),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["id"], "response_ok")
        self.assertEqual(self.store.get(self.first["id"])["state"], "exhausted")
        self.assertIsNotNone(self.store.get(self.second["id"])["last_used_at"])
        self.assertEqual(self.store.group_config("default")["active_id"], self.second["id"])

    def test_mcp_reuses_zcode_group_and_skips_only_the_account_at_its_gate(self):
        start = app.datetime.now(app.timezone.utc)
        for account in (self.first, self.second):
            self.store.update(account["id"], usage_percent=0.0)
            self.store.budget_allows(account["id"], start)
        self.store.update(self.first["id"], usage_percent=5.0)
        self.store.update(self.second["id"], usage_percent=2.0)
        self.assertTrue(all(account["shared"] for account in self.store.public_snapshot("codex-mcp")["accounts"]))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"mcp"}',
            headers={"Authorization": "Bearer " + self.store.agent_config("codex-mcp")["api_key"], "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["id"], "response_ok")
        self.assertIsNone(self.store.get(self.first["id"])["last_used_at"])
        self.assertIsNotNone(self.store.get(self.second["id"])["last_used_at"])

    def test_account_gate_applies_outside_the_mcp_page_and_stops_all_accounts(self):
        now = app.datetime.now(app.timezone.utc)
        for account in (self.first, self.second):
            self.store.budget_allows(account["id"], now)
            self.store.update(account["id"], usage_percent=5.0)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"blocked"}',
            headers={"Authorization": "Bearer " + self.store.api_key(), "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 429)
        self.assertEqual(json.load(error.exception)["error"]["type"], "budget_gate")
        self.assertEqual(len(self.store.agent_config("codex-mcp")["budget_alert"]["accounts"]), 2)

    def test_agent_key_routes_only_to_groups_owned_by_that_agent(self):
        agent = self.store.create_agent("Codex Custom")
        self.store.move(self.second["id"], agent["group_id"])
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"group ping"}',
            headers={
                "Authorization": "Bearer " + self.store.agent_config(agent["id"])["api_key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["id"], "response_ok")
        self.assertIsNone(self.store.get(self.first["id"])["last_used_at"])
        self.assertIsNotNone(self.store.get(self.second["id"])["last_used_at"])
        self.assertEqual(self.store.group_config(agent["group_id"])["active_id"], self.second["id"])

    def test_empty_agent_does_not_fall_back_to_zcode_accounts(self):
        agent = self.store.create_agent("空 Agent")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/models",
            headers={"Authorization": "Bearer " + self.store.agent_config(agent["id"])["api_key"]},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(json.load(error.exception)["error"]["type"], "agent_accounts_unavailable")

    def test_group_order_falls_back_and_disabled_group_is_skipped(self):
        backup = self.store.create_group("备用组", "zcode")
        self.store.move(self.second["id"], backup["id"])
        self.store.toggle_group("default")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"skip disabled"}',
            headers={"Authorization": "Bearer " + self.store.api_key(), "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["id"], "response_ok")
        self.assertEqual(self.store.get(self.first["id"])["state"], "ready")
        self.assertIsNotNone(self.store.get(self.second["id"])["last_used_at"])

    def test_group_order_falls_back_after_primary_group_is_exhausted(self):
        backup = self.store.create_group("备用组", "zcode")
        self.store.move(self.second["id"], backup["id"])
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/responses",
            data=b'{"model":"grok-4.5","input":"ordered fallback"}',
            headers={"Authorization": "Bearer " + self.store.api_key(), "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["id"], "response_ok")
        self.assertEqual(self.store.get(self.first["id"])["state"], "exhausted")
        self.assertEqual(self.store.agent_config("zcode")["active_group_id"], backup["id"])

    def test_provider_rejects_wrong_local_key(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/models",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 401)

    def test_server_disables_address_reuse_for_single_instance_binding(self):
        self.assertFalse(RouterServer.allow_reuse_address)

    def test_route_locks_are_shared_within_an_agent_but_not_between_agents(self):
        self.assertIs(self.server.route_lock("zcode"), self.server.route_lock("zcode"))
        self.assertIsNot(self.server.route_lock("zcode"), self.server.route_lock("hermes"))

    def test_stream_is_rechunked_and_connection_is_reused(self):
        stream_body = b'data: {"delta":"ok"}\n\ndata: [DONE]\n\n'

        def stream_attempt(handler, account, body, refresh):
            return 200, "OK", [("Content-Type", "text/event-stream"), ("Transfer-Encoding", "chunked")], b"", None, io.BytesIO(stream_body)

        Handler._attempt = stream_attempt
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Authorization": "Bearer " + self.store.api_key(), "Content-Type": "application/json"}
        connection.request("POST", "/v1/responses", body=b'{"stream":true}', headers=headers)
        first = connection.getresponse()
        self.assertEqual(first.getheader("Transfer-Encoding"), "chunked")
        self.assertIsNone(first.getheader("Connection"))
        self.assertEqual(first.read(), stream_body)

        first_socket = connection.sock
        connection.request("GET", "/v1/models", headers={"Authorization": "Bearer " + self.store.api_key()})
        second = connection.getresponse()
        self.assertEqual(second.read(), stream_body)
        self.assertIs(connection.sock, first_socket)
        connection.close()

    def test_management_delete_removes_account(self):
        extra = self.store.create("待删除")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/accounts/{extra['id']}",
            method="DELETE",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.load(response)["deleted"])
        self.assertIsNone(self.store.get(extra["id"]))

    def test_management_rename_account_persists_and_rejects_duplicate(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/accounts/{self.first['id']}/rename",
            data=json.dumps({"name": "重新命名"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["name"], "重新命名")
        self.assertEqual(self.store.get(self.first["id"])["name"], "重新命名")

        duplicate = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/accounts/{self.first['id']}/rename",
            data=json.dumps({"name": "第二个"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(duplicate, timeout=5)
        self.assertEqual(error.exception.code, 400)

    def test_account_models_use_selected_account_and_describe_reasoning(self):
        home = self.store.account_home(self.first["id"])
        (home / "auth.json").write_text(json.dumps({"xai": {"key": "selected-token"}}), encoding="utf-8")
        payload = {
            "data": [
                {"id": "grok-4.5"},
                {"id": "grok-4.3"},
                {"id": "grok-4.20-multi-agent-0309"},
                {"id": "grok-4.20-0309-non-reasoning"},
            ]
        }
        with patch.object(app, "open_with_system_proxy", return_value=FakeResponse(payload)) as opener:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.server_port}/api/accounts/{self.first['id']}/models",
                timeout=5,
            ) as response:
                result = json.load(response)
                models = result["models"]
        self.assertEqual(opener.call_args.args[0].headers["Authorization"], "Bearer selected-token")
        self.assertEqual(result["source"], "account")
        self.assertEqual(models[0]["reasoning"], "low / medium / high（默认 high）")
        self.assertIn("默认低", models[1]["reasoning"])
        self.assertIn("Agent 数", models[2]["reasoning"])
        self.assertEqual(models[3]["reasoning"], "固定关闭")

    def test_account_models_fall_back_with_explicit_agent_source_when_selected_quota_is_blocked(self):
        for account, token in ((self.first, "blocked-token"), (self.second, "healthy-token")):
            home = self.store.account_home(account["id"])
            (home / "auth.json").write_text(json.dumps({"xai": {"key": token}}), encoding="utf-8")
        quota = urllib.error.HTTPError(
            "https://api.x.ai/v1/models",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"code":"personal-team-blocked:spending-limit"}'),
        )
        with patch.object(
            app,
            "open_with_system_proxy",
            side_effect=[quota, FakeResponse({"data": [{"id": "grok-4.5"}]})],
        ):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.server_port}/api/accounts/{self.first['id']}/models",
                timeout=5,
            ) as response:
                result = json.load(response)
        self.assertEqual(result["source"], "agent")
        self.assertEqual(result["models"][0]["id"], "grok-4.5")

    def test_account_models_retry_transient_transport_failure(self):
        home = self.store.account_home(self.first["id"])
        (home / "auth.json").write_text(json.dumps({"xai": {"key": "token"}}), encoding="utf-8")
        with patch.object(
            app,
            "open_with_system_proxy",
            side_effect=[urllib.error.URLError("ssl eof"), FakeResponse({"data": [{"id": "grok-4.3"}]})],
        ) as opener, patch.object(app.time, "sleep") as sleep, patch.object(
            app.secrets, "randbelow", return_value=0
        ):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.server_port}/api/accounts/{self.first['id']}/models",
                timeout=5,
            ) as response:
                result = json.load(response)
        self.assertEqual(result["models"][0]["id"], "grok-4.3")
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_management_agent_and_group_lifecycle(self):
        create = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/agents",
            data=json.dumps({"name": "Custom Agent"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(create, timeout=5) as response:
            agent = json.load(response)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.server.server_port}/api/config?agent_id={agent['id']}", timeout=5
        ) as response:
            config = json.load(response)
        self.assertEqual(config["agent"]["name"], "Custom Agent")
        self.assertIn(agent["id"][:8], config["integrations"]["zcode"]["content"])

        add_group = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/groups",
            data=json.dumps({"name": "备用组", "agent_id": agent["id"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(add_group, timeout=5) as response:
            group = json.load(response)
        toggle = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/groups/{group['id']}/toggle",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(toggle, timeout=5) as response:
            self.assertFalse(json.load(response)["enabled"])
        delete = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/groups/{group['id']}", method="DELETE"
        )
        with urllib.request.urlopen(delete, timeout=5) as response:
            self.assertTrue(json.load(response)["deleted"])
        delete_agent = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/agents/{agent['id']}", method="DELETE"
        )
        with urllib.request.urlopen(delete_agent, timeout=5) as response:
            self.assertTrue(json.load(response)["deleted"])

    def test_delete_consumes_body_before_next_keep_alive_request(self):
        extra = self.store.create("带请求体删除")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(
            "DELETE",
            f"/api/accounts/{extra['id']}",
            body=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "2"},
        )
        first = connection.getresponse()
        first.read()
        self.assertEqual(first.status, 200)
        connection.request("GET", "/api/accounts")
        second = connection.getresponse()
        payload = json.loads(second.read())
        connection.close()
        self.assertEqual(second.status, 200)
        self.assertNotIn(extra["id"], [account["id"] for account in payload["accounts"]])


if __name__ == "__main__":
    unittest.main()
