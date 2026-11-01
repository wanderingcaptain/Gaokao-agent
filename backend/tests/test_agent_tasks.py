import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, User, UserProfile, AgentTask, AgentTurn
from routers import agent
from services.auth_service import get_current_user
from test_agent_service import _profile


class AgentTaskTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.patch = patch.object(agent, "SessionLocal", self.sessions)
        self.patch.start()
        self.app = FastAPI()
        self.app.include_router(agent.router)
        self.user = User(id=1, username="alice")
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        with self.sessions() as db:
            db.add(UserProfile(user_id=1, profile_uuid="p", profile_json=json.dumps(_profile()), created_at="now"))
            db.commit()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        for worker in list(agent.workers.values()):
            worker.cancel()
        await asyncio.gather(*list(agent.workers.values()), return_exceptions=True)
        agent.workers.clear()
        await self.client.aclose()
        self.patch.stop()
        self.engine.dispose()

    async def create(self):
        response = await self.client.post("/api/v1/agent/tasks", json={"profile_uuid": "p"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    async def test_wait_resume_idempotency_and_versions(self):
        task_id = await self.create()
        calls = []
        async def fake(profile, **kwargs):
            calls.append(list(kwargs["conversation"]))
            if len(calls) == 1:
                kwargs["conversation"].append({"role": "assistant", "content": "是否更重视稳妥？"})
                yield {"event": "waiting", "data": {"status": "waiting", "question": "是否更重视稳妥？"}}
            else:
                yield {"event": "result", "data": {"status": "completed", "candidates": [], "fallback": False}}
        path = f"/api/v1/agent/tasks/{task_id}"
        with patch.object(agent, "run_recommendation_agent", fake):
            first = {"request_id": "a", "content": "帮我推荐"}
            response = await self.client.post(path + "/messages", json=first)
            self.assertIn("event: waiting", response.text)
            retry = await self.client.post(path + "/messages", json=first)
            self.assertEqual(retry.text, response.text)
            self.assertEqual(len(calls), 1)
            conflict = await self.client.post(path + "/messages", json={**first, "content": "不同内容"})
            self.assertEqual(conflict.status_code, 409)
            result = await self.client.post(path + "/messages", json={"request_id": "b", "content": "是"})
            self.assertIn("event: result", result.text)
            self.assertIn({"role": "assistant", "content": "是否更重视稳妥？"}, calls[-1])
            await self.client.post(path + "/messages", json={"request_id": "c", "content": "再调整"})
        state = (await self.client.get(path)).json()
        self.assertEqual(state["status"], "completed")
        self.assertEqual([p["version"] for p in state["state"]["plans"]], [1, 2])

    async def test_ownership_and_active_turn_conflict(self):
        task_id = await self.create()
        path = f"/api/v1/agent/tasks/{task_id}"
        self.user = User(id=2, username="bob")
        self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.client.post(path + "/cancel")).status_code, 404)
        self.assertEqual((await self.client.post(path + "/messages", json={"request_id": "x", "content": "hi"})).status_code, 404)
        self.user = User(id=1, username="alice")
        with self.sessions() as db:
            db.get(AgentTask, task_id).active_turn_id = "active"
            db.commit()
        response = await self.client.post(path + "/messages", json={"request_id": "x", "content": "hi"})
        self.assertEqual(response.status_code, 409)

    async def test_explicit_profile_update_creates_version(self):
        task_id = await self.create()
        received = []
        async def fake(profile, **kwargs):
            received.append(profile)
            yield {"event": "waiting", "data": {"status": "waiting", "question": "继续吗？"}}
        profile = {"province": "广东", "subject_type": "物理类", "gaokao_score": 600,
                   "province_rank": 20000, "excluded_cities": ["广州"]}
        path = f"/api/v1/agent/tasks/{task_id}"
        with patch.object(agent, "run_recommendation_agent", fake):
            response = await self.client.post(path + "/messages", json={
                "request_id": "change", "content": "排除广州", "profile": profile})
        self.assertEqual(response.status_code, 200)
        state = (await self.client.get(path)).json()["state"]
        self.assertEqual(len(state["profiles"]), 2)
        self.assertEqual(state["profiles"][0]["constraints"]["excluded_cities"], [])
        self.assertEqual(received[0]["constraints"]["excluded_cities"], ["广州"])
        self.assertEqual((await self.client.post(path + "/messages", json={
            "request_id": "invalid", "content": "test", "profile": {**profile, "province_rank": 0}
        })).status_code, 422)

    async def test_cancel_and_restart_recovery(self):
        task_id = await self.create()
        with self.sessions() as db:
            task = db.get(AgentTask, task_id)
            task.active_turn_id = "turn"
            task.status = "running"
            db.add(AgentTurn(id="turn", task_id=task_id, request_id="x", status="running",
                             input_json="{}", events_json="[]", created_at="now"))
            db.commit()
        agent.recover_interrupted()
        state = (await self.client.get(f"/api/v1/agent/tasks/{task_id}")).json()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["turns"][0]["events"][-1]["data"]["status"], "failed")
        # Cancel a live worker rather than merely changing the database flag.
        with self.sessions() as db:
            db.get(AgentTask, task_id).active_turn_id = "turn"
            db.get(AgentTurn, "turn").status = "running"
            db.commit()
        started = asyncio.Event()
        async def waiting_worker():
            started.set()
            await asyncio.sleep(60)
        worker = asyncio.create_task(waiting_worker())
        agent.workers[task_id] = worker
        await started.wait()
        response = await self.client.post(f"/api/v1/agent/tasks/{task_id}/cancel")
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertTrue(worker.cancelled())
        state = (await self.client.get(f"/api/v1/agent/tasks/{task_id}")).json()
        self.assertEqual(state["status"], "cancelled")
