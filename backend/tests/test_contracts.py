import unittest
from pydantic import ValidationError
from contracts.artifacts import EvidencePack, PlanDraft, ReviewReport, Subtask, validate_handoff


class ContractTests(unittest.TestCase):
    def test_old_and_new_import_paths_share_implementation(self):
        from services import rule_engine, agent_service, llm_service
        from tools import risk, candidates, recommendation
        self.assertIs(rule_engine.run_rule_engine, risk.run_rule_engine)
        self.assertIs(agent_service._execute_tool, candidates._execute_tool)
        self.assertIs(llm_service._merge_llm_selection, recommendation._merge_llm_selection)

    def setUp(self):
        self.scope = dict(task_id="task1", profile_version=1, data_version="snapshot1")
        self.evidence = EvidencePack(**self.scope, artifact_id="e1", evidence=[dict(
            evidence_id="fact1", source="db:admissions/1", year=2025, province="广东",
            candidate_ids=[1], claim="历史位次", verification="verified")])
        self.plan = PlanDraft(**self.scope, artifact_id="p1", evidence_pack_id="e1",
            strategy="稳妥", items=[dict(candidate_id=1, reason="匹配", evidence_ids=["fact1"])])

    def test_round_trip_and_handoff(self):
        plan = PlanDraft.model_validate_json(self.plan.model_dump_json())
        validate_handoff(plan, self.evidence, {1})

    def test_wrong_scope_reference_and_pool(self):
        for field, value in (("task_id", "other"), ("profile_version", 2),
                             ("data_version", "other"), ("evidence_pack_id", "missing")):
            with self.assertRaises(ValueError):
                validate_handoff(self.plan.model_copy(update={field: value}), self.evidence, {1})
        with self.assertRaises(ValueError):
            validate_handoff(self.plan, self.evidence, {2})
        self.plan.items[0].evidence_ids = ["missing"]
        with self.assertRaises(ValueError):
            validate_handoff(self.plan, self.evidence, {1})

    def test_review_consistency(self):
        with self.assertRaises(ValidationError):
            ReviewReport(**self.scope, artifact_id="r1", plan_id="p1", verdict="pass",
                issues=[dict(severity="blocking", description="资格未知", requested_action="补查")])
        wrong = ReviewReport(**self.scope, artifact_id="r1", plan_id="p2", verdict="pass")
        with self.assertRaises(ValueError):
            validate_handoff(self.plan, self.evidence, {1}, wrong)

    def test_subtask_and_strict_schema(self):
        with self.assertRaises(ValidationError):
            Subtask(**self.scope, subtask_id="s1", agent="planner", goal="推荐", dependencies=["s1"])
        with self.assertRaises(ValidationError):
            PlanDraft(**self.scope, artifact_id="p2", evidence_pack_id="e1", strategy="test",
                items=[dict(candidate_id=True, reason="test", evidence_ids=["fact1"])])
        with self.assertRaises(ValidationError):
            EvidencePack(**self.scope, artifact_id="e2", fabricated_field=True)

    def test_duplicate_candidates_rejected(self):
        data = self.plan.model_dump()
        data["items"] *= 2
        with self.assertRaises(ValidationError):
            PlanDraft.model_validate(data)
