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

    def test_excluded_city_matches_administrative_suffix(self):
        self._add_records("计算机科学与技术", [19500, 20000, 20500])
        profile = self._profile()
        profile["constraints"]["excluded_cities"] = ["广州市"]
        self.assertEqual(run_rule_engine(self.db, profile), [])

    def test_disliked_major_direction_filter_applies(self):
        self._add_records("计算机科学与技术", [19500, 20000, 20500])
        result = run_rule_engine(self.db, self._profile(disliked_subjects=["计算机类"]))

        self.assertEqual(result, [])

    def test_uses_latest_actual_years_and_honors_target_year(self):
        for year, rank in [(2022, 40000), (2024, 18000), (2025, 20000), (2026, 22000)]:
            self.db.add(AdmissionRecord(university_id="10001", university_name="测试大学",
                major_name="计算机", enrollment_type="普通类", province="广东",
                subject_type="物理类", year=year, min_rank=rank))
        self.db.commit()
        current = run_rule_engine(self.db, self._profile())[0]
        self.assertEqual([row["year"] for row in current["admission_history"]], [2024, 2025, 2026])
        self.assertEqual(current["reference_rank"], 20600)
        profile = self._profile()
        profile["basic_info"]["target_year"] = 2026
        earlier = run_rule_engine(self.db, profile)[0]
        self.assertEqual([row["year"] for row in earlier["admission_history"]], [2022, 2024, 2025])

    def test_sino_foreign_school_flag_and_invalid_user_rank(self):
        self._add_records("计算机", [20000, 20000, 20000])
        self.db.get(University, "10001").is_sino_foreign = 1
        self.db.commit()
        self.assertEqual(run_rule_engine(self.db, self._profile()), [])
        profile = self._profile()
        profile["constraints"]["accept_sino_foreign"] = True
        self.assertEqual(len(run_rule_engine(self.db, profile)), 1)
        profile["basic_info"]["province_rank"] = 0
        with self.assertRaises(ValueError):
            run_rule_engine(self.db, profile)


if __name__ == "__main__":
    unittest.main()
