import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.agent_service import AgentState, _candidate_page, _execute_tool, _validate_state, run_recommendation_agent


def _candidate(candidate_id, tier="稳"):
    return {
        "candidate_id": candidate_id,
        "university_name": f"测试大学{candidate_id}",
        "major_name": "计算机科学与技术",
        "city": "广州",
        "university_type": "普通本科",
        "enrollment_type": "普通类",
        "tier": tier,
        "risk_ratio": 1.0,
        "risk_score": 70,
        "tier_reason": "测试",
    }


def _profile():
    return {
        "basic_info": {
            "province": "广东",
            "subject_type": "物理类",
            "gaokao_score": 600,
            "province_rank": 20000,
        },
        "academic_preferences": {
            "liked_subjects": ["计算机类"],
            "disliked_subjects": [],
            "math_level": "中",
            "english_level": "中",
            "physics_level": "中",
        },
        "personality": {"traits": [], "career_vision": ""},
        "constraints": {
            "annual_budget_min": 0,
            "annual_budget_max": 150000,
            "preferred_cities": [],
            "excluded_cities": [],
            "accept_sino_foreign": False,
            "accept_postgrad": True,
        },
        "priority_factors": [],
        "preference_tree": [],
        "extra_notes": "",
    }


class FakeAgentClient:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_kwargs):
        name, arguments = next(self.calls)
        function = SimpleNamespace(name=name, arguments=arguments)
        call = SimpleNamespace(id=f"call-{name}", function=function)
        message = SimpleNamespace(content="", tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class AgentServiceTest(unittest.TestCase):
    def test_validation_feedback_causes_repair(self):
        pool = [_candidate(1, "冲"), _candidate(2, "稳"), _candidate(3, "保")]
        def row(i):
            return {"candidate_id": i, "fit_score": 80, "reason": "匹配",
                    "warnings": [], "strategy_note": "参考历史"}
        calls = [
            ("get_candidate_page", "{}"),
            ("submit_recommendation", json.dumps({"candidates": [row(2)]})),
            ("validate_recommendation", "{}"),
            ("submit_recommendation", json.dumps({"candidates": [row(i) for i in (1, 2, 3)]})),
            ("validate_recommendation", "{}"),
        ]
        async def collect():
            with patch("services.agent_service.run_rule_engine", return_value=pool), \
                 patch("services.agent_service._async_client", return_value=FakeAgentClient(calls)):
                return [e async for e in run_recommendation_agent(_profile(), None)]
        result = asyncio.run(collect())[-1]["data"]
        validations = [t["validation"] for t in result["trace"] if t["validation"]]
        self.assertFalse(validations[0]["valid"])
        self.assertTrue(validations[1]["valid"])
        self.assertEqual(result["status"], "completed")

    def test_ask_and_comparison_are_different_terminal_paths(self):
        async def collect(calls, history):
            with patch("services.agent_service.run_rule_engine", return_value=[_candidate(1)]), \
                 patch("services.agent_service._async_client", return_value=FakeAgentClient(calls)):
                return [e async for e in run_recommendation_agent(_profile(), None, conversation=history)]
        history = []
        waiting = asyncio.run(collect([("ask_user", '{"question":"城市是硬要求吗？"}')], history))
        self.assertEqual(waiting[-1]["event"], "waiting")
        self.assertEqual(history[-1]["content"], "城市是硬要求吗？")
        answer = asyncio.run(collect([
            ("get_candidate_details", '{"candidate_ids":[1]}'),
            ("answer_comparison", '{"candidate_ids":[1],"summary":"仅有这所学校符合条件"}')], history))
        self.assertEqual(answer[-1]["event"], "answer")
        self.assertEqual(answer[-1]["data"]["sources"][0]["candidate_id"], 1)

    def test_tool_schema_rejects_bad_arguments_and_tier_override(self):
        async def run():
            bad = [
                ("get_candidate_page", []),
                ("get_candidate_page", {"limit": -1}),
                ("get_candidate_details", {"candidate_ids": [True]}),
                ("submit_recommendation", {"candidates": [{"candidate_id": 1, "tier": "保"}]}),
            ]
            for name, args in bad:
                result = await _execute_tool(name, args, self.state)
                self.assertFalse(result["ok"])
        asyncio.run(run())

    def test_step_limit_is_degraded_not_completed(self):
        async def collect():
            with patch("services.agent_service.run_rule_engine", return_value=[_candidate(1)]), \
                 patch("services.agent_service.AGENT_MAX_STEPS", 1), \
                 patch("services.agent_service._async_client",
                       return_value=FakeAgentClient([("get_profile", "{}")])):
                return [e async for e in run_recommendation_agent(_profile(), None)]
        result = asyncio.run(collect())[-1]["data"]
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["fallback"])

    def setUp(self):
        self.state = AgentState(profile={"basic_info": {}, "constraints": {}})
        self.state.candidates = [_candidate(1, "冲"), _candidate(2, "稳"), _candidate(3, "保")]

    def test_candidate_page_only_exposes_rule_candidates(self):
        page = _candidate_page(self.state, {"tier": "稳", "offset": 0, "limit": 10})

        self.assertEqual(page["returned"], 1)
        self.assertEqual(page["candidates"][0]["candidate_id"], 2)
        self.assertEqual(page["candidates"][0]["tier"], "稳")

    def test_unknown_candidate_ids_are_rejected(self):
        result = asyncio.run(_execute_tool(
            "get_candidate_details",
            {"candidate_ids": [999]},
            self.state,
        ))

        self.assertFalse(result["ok"])
        self.assertIn("候选不存在", result["error"])

    def test_validation_rejects_candidate_outside_hard_screen(self):
        self.state.final_candidates = [_candidate(999)]

        result = _validate_state(self.state)

        self.assertFalse(result["valid"])
        self.assertIn("硬筛候选池之外", " ".join(result["errors"]))

    def test_validation_accepts_complete_result_from_hard_screen(self):
        self.state.final_candidates = []
        for candidate_id, tier in [(1, "冲"), (2, "稳"), (3, "保")]:
            item = _candidate(candidate_id, tier)
            item.update({
                "fit_score": 88,
                "reason": "与画像匹配",
                "warnings": [],
                "strategy_note": "放在合适位置",
            })
            self.state.final_candidates.append(item)

        result = _validate_state(self.state)

        self.assertTrue(result["valid"])

    def test_agent_completes_tool_call_loop_without_real_llm(self):
        candidates = [_candidate(1, "冲"), _candidate(2, "稳"), _candidate(3, "保")]
        rows = [
            {
                "candidate_id": item["candidate_id"],
                "fit_score": 88,
                "reason": "与画像匹配",
                "warnings": [],
                "strategy_note": "结合风险层级安排",
            }
            for item in candidates
        ]
        calls = [
            ("run_rule_screening", "{}"),
            ("get_candidate_page", '{"limit": 60}'),
            ("submit_recommendation", json.dumps({"candidates": rows}, ensure_ascii=False)),
            ("validate_recommendation", "{}"),
        ]

        async def collect():
            with patch("services.agent_service.run_rule_engine", return_value=candidates), \
                 patch("services.agent_service._async_client", return_value=FakeAgentClient(calls)):
                return [event async for event in run_recommendation_agent(_profile(), None)]

        events = asyncio.run(collect())
        result_events = [event for event in events if event["event"] == "result"]

        self.assertEqual(len(result_events), 1)
        result = result_events[0]["data"]
        self.assertFalse(result["fallback"])
        self.assertTrue(result["agent"]["validation"]["valid"])
        self.assertEqual(
            result["agent"]["tools_used"],
            ["run_rule_screening", "get_candidate_page", "submit_recommendation", "validate_recommendation"],
        )

    def test_agent_falls_back_when_api_key_is_missing(self):
        candidates = [_candidate(1, "冲"), _candidate(2, "稳"), _candidate(3, "保")]

        async def collect():
            with patch("services.agent_service.run_rule_engine", return_value=candidates), \
                 patch("services.agent_service._async_client", side_effect=ValueError("未配置 QWEN_API_KEY")):
                return [event async for event in run_recommendation_agent(_profile(), None)]

        events = asyncio.run(collect())
        result = [event for event in events if event["event"] == "result"][0]["data"]

        self.assertTrue(result["fallback"])
        self.assertEqual(len(result["candidates"]), 3)


if __name__ == "__main__":
    unittest.main()
