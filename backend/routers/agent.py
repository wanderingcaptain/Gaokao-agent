"""持久化 Agent 任务、幂等轮次、可重放 SSE 与取消。单进程运行。"""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import update

from database.models import AgentTask, AgentTurn, SessionLocal, User, UserProfile, RecommendationResult
from services.auth_service import get_current_user, now_iso
from services.agent_service import run_recommendation_agent
from routers.profile import ProfileCreateRequest
from services.profile_service import build_profile

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])
workers: dict[str, asyncio.Task] = {}


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_uuid: str
    goal: str = Field(default="根据我的画像生成志愿方案", min_length=1, max_length=2000)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    # 仅用户在编辑器中显式提交；绝不从模型输出直接更新硬约束。
    profile: ProfileCreateRequest | None = None


def owned(db, task_id, user_id):
    task = db.get(AgentTask, task_id)
    if not task or task.user_id != user_id:
        raise HTTPException(404, "任务不存在")
    return task


@router.post("/tasks")
def create_task(body: CreateTask, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        profile = db.query(UserProfile).filter_by(user_id=user.id, profile_uuid=body.profile_uuid).first()
        if not profile:
            raise HTTPException(404, "画像不存在")
        task_id = str(uuid.uuid4())
        state = {"schema_version": 1, "goal": body.goal, "profiles": [json.loads(profile.profile_json)],
                 "conversation": [], "plans": []}
        db.add(AgentTask(id=task_id, user_id=user.id, profile_uuid=body.profile_uuid,
                        state_json=json.dumps(state, ensure_ascii=False), status="ready", created_at=now_iso()))
        db.commit()
    return {"id": task_id, "status": "ready"}


@router.get("/tasks/{task_id}")
def read_task(task_id: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        task = owned(db, task_id, user.id)
        turns = db.query(AgentTurn).filter_by(task_id=task_id).order_by(AgentTurn.created_at).all()
        return {"id": task.id, "status": task.status, "state": json.loads(task.state_json),
                "turns": [{"id": t.id, "request_id": t.request_id, "status": t.status,
                           "input": json.loads(t.input_json), "events": json.loads(t.events_json)} for t in turns]}


def append_event(turn_id, event):
    with SessionLocal() as db:
        turn = db.get(AgentTurn, turn_id)
        events = json.loads(turn.events_json)
        event["data"].update(task_id=turn.task_id, turn_id=turn_id)
        events.append(event)
        turn.events_json = json.dumps(events, ensure_ascii=False)
        db.commit()


async def execute_turn(task_id, turn_id):
    conversation = None
    try:
        with SessionLocal() as db:
            task = db.get(AgentTask, task_id)
            state = json.loads(task.state_json)
        conversation = state["conversation"]
        previous = state["plans"][-1] if state["plans"] else None
        stream = run_recommendation_agent(state["profiles"][-1], conversation=conversation,
                                          goal=state["goal"], previous=previous)
        try:
            async for event in stream:
                if event["event"] in ("result", "waiting", "error", "answer"):
                    status = event["data"].get("status", "failed")
                    with SessionLocal() as db:
                        task = db.get(AgentTask, task_id)
                        turn = db.get(AgentTurn, turn_id)
                        if task.active_turn_id != turn_id:
                            return
                        if event["event"] == "result":
                            payload = event["data"]
                            payload["version"] = len(state["plans"]) + 1
                            payload["profile_version"] = len(state["profiles"])
                            row = RecommendationResult(user_id=task.user_id, profile_uuid=task.profile_uuid,
                                result_json=json.dumps(payload, ensure_ascii=False),
                                fallback=int(payload.get("fallback", False)),
                                summary=json.dumps({"task_id": task_id, "version": payload["version"],
                                                    "candidate_count": len(payload.get("candidates", []))}),
                                created_at=now_iso())
                            db.add(row)
                            db.flush()
                            payload["result_id"] = row.id
                            state["plans"].append(dict(payload))
                        task.state_json = json.dumps(state, ensure_ascii=False)
                        task.status = status
                        task.active_turn_id = None
                        turn.status = status
                        # 终态与最后一个事件在同一事务内持久化。
                        event["data"].update(task_id=task_id, turn_id=turn_id)
                        events = json.loads(turn.events_json)
                        events.append(event)
                        turn.events_json = json.dumps(events, ensure_ascii=False)
                        db.commit()
                    return
                append_event(turn_id, event)
        finally:
            await stream.aclose()
    except asyncio.CancelledError:
        finish_interrupted(task_id, turn_id, "cancelled", "本轮已取消")
        raise
    except Exception:
        finish_interrupted(task_id, turn_id, "failed", "本轮执行失败，可重新发送消息")
    finally:
        workers.pop(task_id, None)


def finish_interrupted(task_id, turn_id, status, message):
    with SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        turn = db.get(AgentTurn, turn_id)
        if not task or task.active_turn_id != turn_id:
            return
        task.status = status
        task.active_turn_id = None
        turn.status = status
        events = json.loads(turn.events_json)
        events.append({"event": "error", "data": {"status": status, "message": message,
                                                  "task_id": task_id, "turn_id": turn_id}})
        turn.events_json = json.dumps(events, ensure_ascii=False)
        db.commit()


async def replay(turn_id, request):
    offset = 0
    while not await request.is_disconnected():
        with SessionLocal() as db:
            turn = db.get(AgentTurn, turn_id)
            events = json.loads(turn.events_json)
            running = turn.status == "running"
        for event in events[offset:]:
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        offset = len(events)
        if not running:
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(0.5)


@router.post("/tasks/{task_id}/messages")
async def message(task_id: str, body: Message, request: Request, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        task = owned(db, task_id, user.id)
        old = db.query(AgentTurn).filter_by(task_id=task_id, request_id=body.request_id).first()
        if old:
            if json.loads(old.input_json) != body.model_dump():
                raise HTTPException(409, "同一请求编号不能用于不同消息")
            turn_id = old.id
        else:
            turn_id = str(uuid.uuid4())
            locked = db.execute(update(AgentTask).where(
                AgentTask.id == task_id, AgentTask.active_turn_id.is_(None)
            ).values(active_turn_id=turn_id, status="running"))
            if locked.rowcount != 1:
                raise HTTPException(409, "此任务已有活动轮次，请等待或取消")
            state = json.loads(task.state_json)
            if body.profile:
                state["profiles"].append(build_profile(body.profile.model_dump(), uid=task.profile_uuid))
            state["conversation"].append({"role": "user", "content": body.content})
            task.state_json = json.dumps(state, ensure_ascii=False)
            db.add(AgentTurn(id=turn_id, task_id=task_id, request_id=body.request_id,
                            status="running", input_json=body.model_dump_json(), events_json="[]",
                            created_at=now_iso()))
            db.commit()
            workers[task_id] = asyncio.create_task(execute_turn(task_id, turn_id))
    return StreamingResponse(replay(turn_id, request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, user: User = Depends(get_current_user)):
    with SessionLocal() as db:
        task = owned(db, task_id, user.id)
        turn_id = task.active_turn_id
    worker = workers.get(task_id)
    if worker:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    if turn_id:
        finish_interrupted(task_id, turn_id, "cancelled", "本轮已取消")
    return {"status": "cancelled" if turn_id else "idle"}


def recover_interrupted():
    with SessionLocal() as db:
        pending = [(t.id, t.active_turn_id) for t in db.query(AgentTask).filter(AgentTask.active_turn_id.isnot(None))]
    for task_id, turn_id in pending:
        finish_interrupted(task_id, turn_id, "failed", "服务重启中断了本轮，可重新发送消息继续")
