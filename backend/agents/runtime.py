"""Four-role bounded runtime, strict tool permissions, server-owned artifacts.

LangGraph routes coordinator -> specialist -> coordinator. No model can self-grant consent.
"""
import asyncio
import json
import time
from copy import deepcopy
from types import SimpleNamespace
from openai import APIConnectionError, RateLimitError, InternalServerError
from pathlib import Path
from typing import TypedDict
from pydantic import ValidationError
from langgraph.graph import StateGraph, START, END
from providers.client import ModelSettings, create_client, request_options, assistant_message
from agents.schemas import tool_definitions
from tools.research_session import ResearchTools
from tools.planning import effective_profile
from services.qualification import lookup_policies
from agents.research import search_web, query_history


class GraphState(TypedDict, total=False):
    next_role: str


class BudgetExceeded(Exception):
    pass


class TransientFailure(Exception):
    pass


def new_state():
    return {'schema_version': 2, 'profile_version': 1, 'memories': [], 'pending': {},
        'profile': {}, 'artifacts': {}, 'sources': {}, 'candidates': {}, 'plans': [],
        'source_versions': {}, 'conversation': [], 'trace': [], 'requests': {}}


class MultiAgentRuntime(ResearchTools):
    def __init__(self, state, *, client=None, settings=None, search=search_web, query=query_history,
                 max_calls=24, max_searches=8, checkpoint=None, execution=None, persist=None,
                 policies=lookup_policies):
        self.state = state
        # Legacy sessions need a fresh query before old unversioned evidence can be reused.
        self.state.setdefault('source_versions', {})
        self.settings = settings or ModelSettings.from_env()
        self.client = client
        self.search, self.query = search, query
        self.policies = policies
        self.max_calls, self.max_searches = max_calls, max_searches
        self.calls = self.searches = self.tool_calls = 0
        self.authorized = False
        self.messages = []
        self.delegation = None
        self.reply = None
        self.user_text = ''
        self.read_ids = set()
        self.read_source_ids = set()
        self.checkpoint = checkpoint
        self.execution = deepcopy(execution or {})
        self.persist = persist
        self.durable_mode = False
        self.role_messages = {}
        self.started_at = None
        self.base_elapsed = self.execution.get('elapsed_seconds', 0)
        self.recovered = bool(self.execution.get('initialized'))

    def consume_search(self):
        if self.searches >= self.max_searches:
            raise BudgetExceeded('搜索预算用尽')
        self.searches += 1

    async def step(self, role, messages):
        if self.calls >= self.max_calls:
            raise BudgetExceeded('模型调用预算用尽')
        self.calls += 1
        started = time.monotonic()
        if self.durable_mode:
            self.save_execution()
        try:
            response = await self.client.chat.completions.create(messages=messages,
                tools=tool_definitions(role), tool_choice='auto',
                **request_options(self.settings, temperature=.15, max_tokens=3000))
        except Exception as exc:
            if self.durable_mode:
                self.execution.setdefault('metrics', []).append({'step_id': f'model_{self.calls}',
                    'role': role, 'kind': 'model', 'name': self.settings.model, 'ok': False,
                    'duration_ms': (time.monotonic() - started) * 1000,
                    'usage': {}, 'error_code': type(exc).__name__})
                self.save_execution()
            raise
        usage = getattr(response, 'usage', None)
        usage = usage.model_dump() if hasattr(usage, 'model_dump') else {}
        if self.durable_mode:
            self.execution.setdefault('metrics', []).append({'step_id': f'model_{self.calls}',
                'role': role, 'kind': 'model', 'name': self.settings.model, 'ok': True,
                'duration_ms': (time.monotonic() - started) * 1000,
                'usage': {k: usage.get(k, 0) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}})
        message = response.choices[0].message
        messages.append(assistant_message(message))
        return message

    async def tools(self, role, message, messages):
        terminal = None
        for call in message.tool_calls or []:
            self.tool_calls += 1
            if self.tool_calls > 48:
                raise BudgetExceeded('工具调用预算用尽')
            name = call.function.name
            try:
                if terminal is not None:
                    result = {'ok': False, 'error': '本轮已经结束或交接，请在下一轮调用'}
                else:
                    result = await self.execute(role, name, json.loads(call.function.arguments))
                    if result.get('ok') is False:
                        pass
                    elif name == 'delegate_task':
                        self.delegation = {**result, 'call_id': call.id}
                        terminal = result['agent']
                        # Fill placeholder, replace once specialist returns; tool-call pairing remains valid.
                        result = {'status': 'running'}
                    elif name in {'submit_report', 'submit_plan'}:
                        terminal = result
                    elif name in {'respond', 'request_confirmation'}:
                        terminal = 'end'
            except (ValueError, ValidationError, KeyError, TypeError) as exc:
                result = {'ok': False, 'error': str(exc)[:300]}
            except BudgetExceeded:
                raise
            except Exception:
                result = {'ok': False, 'error': '工具服务不可用，请保留未知或说明失败'}
            self.state['trace'].append({'role': role, 'tool': name, 'ok': result.get('ok', True)})
            if self.checkpoint and not self.durable_mode:
                self.checkpoint(self.state)
            messages.append({'role': 'tool', 'tool_call_id': call.id,
                             'content': json.dumps(result, ensure_ascii=False)})
        if not message.tool_calls:
            messages.append({'role': 'user', 'content': '请使用角色工具交付结果、回答或请求确认。'})
        return terminal

    def context(self, role, task=None):
        prompt = (Path(__file__).parent / 'prompts' / f'{role}.md').read_text()
        data = {'memories': self.state['memories'], 'profile': effective_profile(self.state),
                'profile_version': self.state['profile_version'],
                'research_authorized': self.authorized, 'task': task}
        if role == 'coordinator':
            data['artifacts'] = [{k: a.get(k) for k in ('artifact_id', 'role', 'profile_version', 'missing')}
                                 for a in self.state['artifacts'].values()][-30:]
        return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]

    def save_execution(self):
        ex = self.execution
        role = ex.get('role', 'coordinator')
        ex.setdefault('reads', {})[role] = {'artifacts': sorted(self.read_ids), 'sources': sorted(self.read_source_ids)}
        ex.update(calls=self.calls, searches=self.searches, tool_calls=self.tool_calls,
                  authorized=self.authorized, delegation=self.delegation, reply=self.reply,
                  elapsed_seconds=self.base_elapsed + (time.monotonic() - self.started_at if self.started_at else 0))
        if self.persist:
            self.persist(self.state, deepcopy(ex))
        elif self.checkpoint:
            self.checkpoint(self.state)

    def activate(self, role, fresh=False):
        self.execution['role'] = role
        reads = self.execution.setdefault('reads', {}).get(role, {}) if not fresh else {}
        self.read_ids = set(reads.get('artifacts', []))
        self.read_source_ids = set(reads.get('sources', []))
        if fresh or role not in self.role_messages:
            messages = self.context(role, self.delegation if role != 'coordinator' else None)
            # On recovery, reconstruct from tool facts, never persist hidden model reasoning.
            facts = self.execution.get('observations', {}).get(role, [])[-20:] if not fresh else []
            messages.append({'role': 'user', 'content': json.dumps({'user_message': self.user_text,
                'completed_tools': facts, 'resumed': bool(facts)}, ensure_ascii=False)})
            self.role_messages[role] = messages
        if role == 'coordinator':
            self.messages = self.role_messages[role]

    async def advance(self, graph_state):
        ex = self.execution
        role = ex['role']
        self.activate(role)
        messages = self.role_messages[role]
        if ex['phase'] == 'model':
            if role != 'coordinator' and ex.get('specialist_calls', 0) >= 8:
                self.finish_specialist({'status': 'failed', 'error': '子任务未在预算内交付产物'})
                self.save_execution()
                return {'next_role': 'continue'}
            if role != 'coordinator':
                ex['specialist_calls'] = ex.get('specialist_calls', 0) + 1
            message = await self.step(role, messages)
            calls = assistant_message(message).get('tool_calls', [])
            ex.update(phase='tools' if calls else 'model', pending=calls, index=0, terminal=None)
            if not calls:
                messages.append({'role': 'user', 'content': '请调用角色工具交付结果或请求确认。'})
            self.save_execution()
            return {'next_role': 'continue'}
        call = ex['pending'][ex['index']]
        started = time.monotonic()
        if ex.get('terminal') is not None:
            result = {'ok': False, 'error': '本轮已经结束或交接，后续工具未执行'}
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)})
        else:
            before = len(messages)
            message = SimpleNamespace(tool_calls=[SimpleNamespace(id=call['id'],
                function=SimpleNamespace(**call['function']))])
            terminal = await self.tools(role, message, messages)
            result = json.loads(messages[before]['content'])
            if terminal is not None:
                ex['terminal'] = terminal
        ex.setdefault('observations', {}).setdefault(role, []).append(
            {'tool': call['function']['name'], 'call_id': call['id'], 'result': result})
        ex.setdefault('metrics', []).append({'step_id': f"tool_{self.calls}_{ex['index']}",
            'role': role, 'kind': 'tool', 'name': call['function']['name'], 'ok': result.get('ok', True),
            'duration_ms': (time.monotonic() - started) * 1000, 'usage': {},
            'error_code': None if result.get('ok', True) else 'tool_rejected'})
        ex['index'] += 1
        if ex['index'] == len(ex['pending']):
            terminal = ex['terminal']
            ex.update(phase='model', pending=[], index=0, terminal=None)
            ex.setdefault('reads', {})[role] = {'artifacts': sorted(self.read_ids), 'sources': sorted(self.read_source_ids)}
            if self.recovered:
                # Recovered tools are durable commands, not continuations of an in-memory
                # provider reasoning message. The next decision uses a fresh factual context.
                self.role_messages.pop(role, None)
            if role == 'coordinator' and terminal == 'end':
                ex['phase'] = 'done'
            elif role == 'coordinator' and terminal:
                ex['specialist_calls'] = 0
                self.activate(terminal, fresh=True)
                ex.setdefault('observations', {})[terminal] = []
            elif role != 'coordinator' and isinstance(terminal, dict):
                self.finish_specialist(terminal)
        self.save_execution()
        return {'next_role': 'end' if ex['phase'] == 'done' else 'continue'}

    def finish_specialist(self, result):
        subtask = self.state.get('subtasks', {}).get(self.delegation.get('subtask_id'))
        if subtask:
            subtask['status'] = 'completed' if result.get('artifact_id') else 'failed'
            subtask['output_artifact_ids'] = [result['artifact_id']] if result.get('artifact_id') else []
            subtask['error'] = result.get('error')
        call_id = self.delegation['call_id']
        for message in self.role_messages.get('coordinator', []):
            if message.get('role') == 'tool' and message.get('tool_call_id') == call_id:
                message['content'] = json.dumps(result, ensure_ascii=False)
        for observation in self.execution.get('observations', {}).get('coordinator', []):
            if observation['call_id'] == call_id:
                observation['result'] = result
        self.activate('coordinator')
        self.execution.update(phase='model', pending=[], index=0, terminal=None)

    def build_graph(self):
        graph = StateGraph(GraphState)
        graph.add_node('advance', self.advance)
        graph.add_edge(START, 'advance')
        graph.add_conditional_edges('advance', lambda s: s['next_role'], {'continue': 'advance', 'end': END})
        return graph.compile()

    async def run(self, text, confirmed_ids=()):
        if getattr(self, '_has_run', False) and self.execution.get('phase') == 'done':
            self.execution, self.role_messages, self.base_elapsed = {}, {}, 0
            self.recovered = False
        self._has_run = True
        self.user_text = text
        self.durable_mode = True
        self.started_at = time.monotonic()
        if self.execution.get('initialized'):
            self.calls = self.execution.get('calls', 0)
            self.searches = self.execution.get('searches', 0)
            self.tool_calls = self.execution.get('tool_calls', 0)
            self.authorized = self.execution.get('authorized', False)
            self.delegation = self.execution.get('delegation')
            self.reply = self.execution.get('reply')
            self.activate(self.execution['role'])
        else:
            self.authorized = False
            self.reply = None
            self.calls = self.searches = self.tool_calls = 0
            self.read_ids, self.read_source_ids = set(), set()
            if len(confirmed_ids) != len(set(confirmed_ids)):
                raise ValueError('确认编号重复')
            # Confirmation IDs originate from authenticated UI input, never from model tools.
            confirmations = []
            for cid in confirmed_ids:
                pending = self.state['pending'].get(cid)
                if not pending:
                    raise ValueError('确认项不存在或已使用')
                if pending['kind'] == 'hard' and pending.get('profile_version', self.state['profile_version']) != self.state['profile_version']:
                    raise ValueError('硬约束提案已过期，请重新提出')
                confirmations.append((cid, pending))
            budget_values = {p['value'] for _, p in confirmations if p.get('field') == 'annual_budget_max'}
            if len(budget_values) > 1:
                raise ValueError('不能同时确认互相冲突的预算提案')
            resulting_version = self.state['profile_version'] + sum(p['kind'] == 'hard' for _, p in confirmations)
            if any(p['kind'] == 'research' and p['profile_version'] != resulting_version for _, p in confirmations):
                raise ValueError('画像已变化，请重新确认调查范围')
            for cid, pending in confirmations:
                if pending['kind'] == 'hard':
                    self.state['memories'].append({**pending, 'confirmed': True})
                    self.state['profile_version'] += 1
            for cid, pending in confirmations:
                if pending['kind'] == 'research':
                    if pending['profile_version'] != self.state['profile_version']:
                        raise ValueError('画像已变化，请重新确认调查范围')
                    self.authorized = True
                del self.state['pending'][cid]
            self.execution.update(initialized=True, role='coordinator', phase='model', observations={}, metrics=[])
            self.messages = self.context('coordinator') + self.state['conversation'][-20:]
            self.messages.append({'role': 'user', 'content': text})
            self.role_messages['coordinator'] = self.messages
            self.save_execution()
        own_client = self.client is None
        try:
            if own_client:
                self.client = create_client(self.settings)
            remaining = 300 - self.base_elapsed
            if remaining <= 0:
                raise BudgetExceeded('总执行时间已耗尽')
            async with asyncio.timeout(remaining):
                if self.execution.get('phase') != 'done':
                    await self.build_graph().ainvoke({}, {'recursion_limit': 200})
        except (BudgetExceeded, TimeoutError):
            self.reply = {'status': 'degraded', 'text': '本轮预算已耗尽；已完成产物保留，未完成部分不能视为核实。'}
        except (APIConnectionError, RateLimitError, InternalServerError):
            self.save_execution()
            raise TransientFailure('模型服务暂不可用') from None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.reply = {'status': 'failed', 'text': '本轮模型或执行服务不可用，未完成调查。'}
        finally:
            if own_client and self.client is not None:
                try:
                    await self.client.close()
                except Exception:
                    pass
                self.client = None
        if self.reply is None:
            self.reply = {'status': 'failed', 'text': '执行未交付最终回答。'}
        if not self.execution.get('conversation_saved'):
            self.state['conversation'].extend([{'role': 'user', 'content': text},
                {'role': 'assistant', 'content': self.reply['text']}])
            self.execution['conversation_saved'] = True
        self.execution['phase'] = 'done'
        self.save_execution()
        return self.reply
