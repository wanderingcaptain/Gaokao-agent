"""Regression coverage for the legacy report's source data and ownership."""
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AdmissionRecord, AgentTask, Base, University, User, UserProfile, get_db
from routers import detail
from services.auth_service import get_current_user


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        sessions = sessionmaker(bind=self.engine)
        self.profile = {'basic_info': {'province': '广东', 'subject_type': '物理类'}}
        with sessions.begin() as db:
            db.add(UserProfile(user_id=1, profile_uuid='mine', profile_json=json.dumps(self.profile),
                               created_at='2026-11-12'))
            db.add(University(university_id='100', name='数据库院校', province='北京', city='北京市',
                              university_type='普通本科'))
            db.add(AdmissionRecord(university_id='100', university_name='数据库院校', major_name='计算机',
                                   enrollment_type='普通类', province='广东', subject_type='物理类',
                                   year=2025, min_score=620, min_rank=21000))
            db.add(AgentTask(id='task', user_id=1, profile_uuid='mine', created_at='2026-11-12',
                             state_json=json.dumps({'profiles': [self.profile, {
                                 'basic_info': {'province': '浙江', 'subject_type': '物理类'}}]})))

        def database():
            with sessions() as db:
                yield db

        app = FastAPI()
        app.include_router(detail.router)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = lambda: User(id=1)
        self.client = TestClient(app)
        self.payload = {'uuid': 'mine', 'university_id': '100', 'major_name': '计算机',
                        'university_data': {'enrollment_type': '普通类', 'university_name': '伪造院校',
                                            'admission_history': [{'min_rank': 1}]}}

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_report_uses_database_snapshot_and_matching_profile_scope(self):
        path = '/api/v1/detail/ai-report'
        with patch.object(detail, 'generate_detail_ui', return_value={'html': '<p>参考</p>', 'chartConfig': {}}) as model:
            response = self.client.post(path, json={**self.payload, 'task_id': 'task', 'profile_version': 1})
            self.assertEqual(response.status_code, 200)
            profile, candidate = model.call_args.args
            self.assertEqual(profile, self.profile)
            self.assertEqual(candidate['university_name'], '数据库院校')
            self.assertEqual(candidate['admission_history'][0]['min_rank'], 21000)

            other_province = self.client.post(path, json={**self.payload, 'task_id': 'task', 'profile_version': 2})
            self.assertEqual(other_province.status_code, 404)
            self.assertEqual(self.client.post(path, json={**self.payload, 'uuid': 'other'}).status_code, 404)
            model.assert_awaited_once()
