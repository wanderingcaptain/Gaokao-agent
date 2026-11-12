"""Database-backed worker. Leases fence stale workers; checkpoints and output commit together."""
import asyncio
import json
import os
import time
from uuid import uuid4
from sqlalchemy import select, update, or_
from database.models import AgentRun, MultiAgentSession, RunStep, SessionLocal, SubtaskRecord
from services.run_store import encode, event, load_state, save_state, record_outcome, TERMINAL
from agents.runtime import MultiAgentRuntime, TransientFailure


class LeaseLost(BaseException):
    pass


class RunCancelled(BaseException):
    pass


class RunWorker:
    def __init__(self, sessions=SessionLocal, runtime_factory=MultiAgentRuntime, *, lease_seconds=30, max_attempts=3):
        self.sessions, self.runtime_factory = sessions, runtime_factory
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.owner = uuid4().hex
        self.stopping = False

    def claim(self):
        now = time.time()
        with self.sessions() as db:
            candidates = db.scalars(select(AgentRun).where(or_(AgentRun.status == 'queued',
                (AgentRun.status == 'running') & (AgentRun.lease_until < now))).order_by(AgentRun.created_at).limit(10)).all()
            for run in candidates:
                changed = db.execute(update(AgentRun).where(AgentRun.id == run.id,
                    or_(AgentRun.status == 'queued', (AgentRun.status == 'running') & (AgentRun.lease_until < now)))
                    .values(status='running', owner=self.owner, lease_until=now + self.lease_seconds,
                            attempts=AgentRun.attempts + 1, updated_at=now))
                if changed.rowcount:
                    event(db, run.id, 'running', {'status': 'running', 'resumed': bool(run.checkpoint_json != '{}')})
                    db.commit()
                    return run.id
            db.rollback()
        return None

    def persist(self, run_id, state, execution):
        now = time.time()
        with self.sessions() as db:
            # UPDATE obtains the write lock before reading or publishing any artifacts.
            fenced = db.execute(update(AgentRun).where(AgentRun.id == run_id,
                AgentRun.owner == self.owner, AgentRun.status == 'running',
                AgentRun.lease_until >= now, AgentRun.cancel_requested == 0).values(updated_at=now))
            if not fenced.rowcount:
                run = db.get(AgentRun, run_id)
                if run and run.cancel_requested:
                    raise RunCancelled()
                raise LeaseLost()
            run = db.get(AgentRun, run_id)
            session = db.get(MultiAgentSession, run.session_id)
            from services.knowledge import sync_memories
            sync_memories(db, run.user_id, state)
            save_state(db, session, state, run_id)
            run.checkpoint_json = encode(execution)
            for metric in execution.get('metrics', []):
                if db.get(RunStep, (run_id, metric['step_id'])):
                    continue
                db.add(RunStep(run_id=run_id, step_id=metric['step_id'], role=metric['role'],
                    kind=metric['kind'], name=metric['name'], ok=int(metric['ok']),
                    duration_ms=metric['duration_ms'], usage_json=encode(metric.get('usage', {})),
                    error_code=metric.get('error_code')))
                event(db, run_id, 'progress', {k: metric[k] for k in ('role', 'kind', 'name', 'ok', 'duration_ms')})
            db.commit()

    def finish(self, run_id, result):
        with self.sessions() as db:
            changed = db.execute(update(AgentRun).where(AgentRun.id == run_id,
                AgentRun.owner == self.owner, AgentRun.status == 'running',
                AgentRun.lease_until >= time.time()).values(updated_at=time.time()))
            if not changed.rowcount:
                return
            run = db.get(AgentRun, run_id)
            if run.cancel_requested:
                result = {'status': 'cancelled', 'text': '任务已取消，已保存结果仍可查看。'}
            run.status = result['status']
            run.result_json = encode(result)
            run.owner = None
            run.lease_until = 0
            session = db.get(MultiAgentSession, run.session_id)
            if session.active_request_id == run.request_id:
                session.active_request_id = None
            record_outcome(db, run, session, result)
            for subtask in db.scalars(select(SubtaskRecord).where(SubtaskRecord.run_id == run_id,
                    SubtaskRecord.status == 'running')):
                body = json.loads(subtask.body_json)
                body.update(status='cancelled' if result['status'] == 'cancelled' else 'failed',
                            error='父任务结束，子任务未完成')
                subtask.status, subtask.body_json = body['status'], encode(body)
            event(db, run_id, 'result', result)
            db.commit()

    def release(self, run_id, reason):
        with self.sessions() as db:
            changed = db.execute(update(AgentRun).where(AgentRun.id == run_id,
                AgentRun.owner == self.owner, AgentRun.status == 'running').values(
                status='queued', owner=None, lease_until=0, updated_at=time.time()))
            if changed.rowcount:
                event(db, run_id, 'queued', {'status': 'queued', 'reason': reason})
            db.commit()

    async def heartbeat(self, run_id, task):
        while not task.done():
            await asyncio.sleep(max(.05, self.lease_seconds / 3))
            with self.sessions() as db:
                changed = db.execute(update(AgentRun).where(AgentRun.id == run_id,
                    AgentRun.owner == self.owner, AgentRun.status == 'running', AgentRun.cancel_requested == 0)
                    .values(lease_until=time.time() + self.lease_seconds))
                db.commit()
                if not changed.rowcount:
                    task.cancel()
                    return

    async def execute_claimed(self, run_id):
        with self.sessions() as db:
            run = db.get(AgentRun, run_id)
            state = load_state(db, db.get(MultiAgentSession, run.session_id))
            state['run_id'] = run.id
            payload, execution = json.loads(run.input_json), json.loads(run.checkpoint_json)
            cancelled, attempts = run.cancel_requested, run.attempts
        if cancelled or attempts > self.max_attempts:
            self.finish(run_id, {'status': 'cancelled' if cancelled else 'failed',
                'text': '任务已取消。' if cancelled else '恢复或重试次数已用尽，已保存结果仍可查看。'})
            return
        runtime = self.runtime_factory(state, execution=execution,
            persist=lambda current, cursor: self.persist(run_id, current, cursor))
        runtime.history_candidate_id = payload.get('history_candidate_id')
        runtime.history_plan = bool(payload.get('history_plan'))
        task = asyncio.create_task(runtime.run(payload['text'], payload.get('confirmed_ids', [])))
        heartbeat = asyncio.create_task(self.heartbeat(run_id, task))
        try:
            result = await task
            self.finish(run_id, result)
        except TransientFailure as exc:
            if attempts < self.max_attempts:
                self.release(run_id, 'provider_retry')
            else:
                self.finish(run_id, {'status': 'failed', 'text': str(exc), 'provider_error': exc.reason})
        except (RunCancelled, asyncio.CancelledError):
            if self.stopping:
                self.release(run_id, 'worker_shutdown')
                raise asyncio.CancelledError()
            with self.sessions() as db:
                run = db.get(AgentRun, run_id)
                is_cancelled = bool(run and run.cancel_requested)
            if is_cancelled:
                self.finish(run_id, {'status': 'cancelled', 'text': '任务已取消。'})
            else:
                self.release(run_id, 'execution_interrupted')
        except LeaseLost:
            return  # The replacement worker owns all subsequent writes.
        except Exception:
            self.finish(run_id, {'status': 'failed', 'text': '执行服务异常，已保存结果保留。'})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def run_once(self):
        run_id = self.claim()
        if run_id:
            try:
                await self.execute_claimed(run_id)
            except Exception:
                self.finish(run_id, {'status': 'failed', 'text': '任务配置或数据无效。'})
        return run_id

    async def serve(self):
        while not self.stopping:
            if not await self.run_once():
                await asyncio.sleep(.5)


async def main():
    from database.models import init_db
    init_db()
    await RunWorker().serve()


if __name__ == '__main__':
    asyncio.run(main())
