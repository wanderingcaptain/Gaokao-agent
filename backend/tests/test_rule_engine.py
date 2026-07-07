import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AdmissionRecord, Base, University
from services.rule_engine import _classify_tier, run_rule_engine


class RuleEngineTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(University(
            university_id="10001",
            name="测试大学",
            province="广东",
            city="广州",
            university_type="普通本科",
            is_sino_foreign=0,
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _profile(self, disliked_subjects=None):
        return {
            "basic_info": {
                "province": "广东",
                "subject_type": "物理类",
                "gaokao_score": 600,
                "province_rank": 20000,
            },
            "academic_preferences": {
                "disliked_subjects": disliked_subjects or [],
            },
            "constraints": {
                "excluded_cities": [],
                "accept_sino_foreign": False,
                "preferred_cities": [],
            },
        }

    def _add_records(self, major_name, ranks):
        for year, rank in zip([2023, 2024, 2025], ranks):
            self.db.add(AdmissionRecord(
                university_id="10001",
                university_name="测试大学",
                major_name=major_name,
                major_category="工学",
                enrollment_type="普通类",
                province="广东",
                subject_type="物理类",
                year=year,
                min_score=600,
                min_rank=rank,
                enrollment_count=20,
            ))
        self.db.commit()

    def test_tier_boundaries(self):
        cases = [
            (17000 / 20000, "冲"),
            (19000 / 20000, "稳"),
            (20000 / 20000, "稳"),
            (21800 / 20000, "稳"),
            (24000 / 20000, "保"),
        ]
        for ratio, expected in cases:
            with self.subTest(ratio=ratio):
                tier, _, _ = _classify_tier(ratio)
                self.assertEqual(tier, expected)

    def test_missing_avg_score_falls_back_to_min_rank(self):
        self._add_records("计算机科学与技术", [19500, 20000, 20500])
        result = run_rule_engine(self.db, self._profile())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tier"], "稳")
        self.assertEqual(result[0]["risk_basis"], "min_rank")
        self.assertIsNone(result[0]["avg_score_rank"])

    def test_disliked_subject_filter_still_applies(self):
        self._add_records("英语翻译", [19500, 20000, 20500])
        result = run_rule_engine(self.db, self._profile(disliked_subjects=["英语"]))

        self.assertEqual(result, [])

    def test_disliked_major_direction_filter_applies(self):
        self._add_records("计算机科学与技术", [19500, 20000, 20500])
        result = run_rule_engine(self.db, self._profile(disliked_subjects=["计算机类"]))

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
