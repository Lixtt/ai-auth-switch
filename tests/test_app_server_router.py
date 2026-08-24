from __future__ import annotations

import unittest

from ai_auth_switch.app_server_router import AppServerRouteTable
from ai_auth_switch.errors import AiAuthSwitchError


class AppServerRouterTests(unittest.TestCase):
    def test_new_thread_is_selected_then_response_binds_thread(self) -> None:
        routes = AppServerRouteTable()
        plan = routes.plan_request("thread/start", {"model": "gpt-5"}, 1)
        self.assertEqual(plan.kind, "select")
        assigned = routes.assign_new_thread(1, "profile-a")
        self.assertEqual(assigned.backend, "profile-a")
        routes.record_backend_response(
            "profile-a",
            1,
            {"result": {"thread": {"id": "thr-1"}}},
        )
        turn = routes.plan_request("turn/start", {"threadId": "thr-1"}, 2)
        self.assertEqual(turn.backend, "profile-a")

    def test_fork_and_resume_follow_source_thread_backend(self) -> None:
        routes = AppServerRouteTable()
        routes.bind_thread("thr-1", "profile-b")
        resume = routes.plan_request("thread/resume", {"threadId": "thr-1"}, 3)
        fork = routes.plan_request("thread/fork", {"threadId": "thr-1"}, 4)
        self.assertEqual(resume.backend, "profile-b")
        self.assertEqual(fork.backend, "profile-b")

    def test_unknown_thread_requires_discovery(self) -> None:
        routes = AppServerRouteTable()
        plan = routes.plan_request("turn/start", {"threadId": "missing"}, 1)
        self.assertEqual(plan.kind, "discover")
        self.assertEqual(plan.route_key, "thread:missing")

    def test_thread_list_is_an_aggregate_operation(self) -> None:
        routes = AppServerRouteTable(control_backend="profile-a")
        plan = routes.plan_request("thread/list", {}, 1)
        self.assertEqual(plan.kind, "aggregate")
        self.assertTrue(plan.aggregate)

    def test_backend_server_request_ids_are_namespaced_and_restored(self) -> None:
        routes = AppServerRouteTable()
        request = routes.forward_server_request(
            "profile-a",
            {"method": "item/commandExecution/requestApproval", "id": 9},
        )
        self.assertEqual(request.forwarded_id, "pool:profile-a:int:9")
        backend, restored = routes.route_server_response(
            {"id": request.forwarded_id, "result": {"decision": "accept"}}
        )
        self.assertEqual(backend, "profile-a")
        self.assertEqual(restored["id"], 9)

    def test_unknown_server_response_is_rejected(self) -> None:
        routes = AppServerRouteTable()
        with self.assertRaisesRegex(AiAuthSwitchError, "unknown pool"):
            routes.route_server_response({"id": "pool:missing"})

    def test_thread_started_notification_binds_route(self) -> None:
        routes = AppServerRouteTable()
        routes.record_backend_notification(
            "profile-c",
            {"method": "thread/started", "params": {"thread": {"id": "thr-3"}}},
        )
        self.assertEqual(routes.backend_for_thread("thr-3"), "profile-c")


if __name__ == "__main__":
    unittest.main()
