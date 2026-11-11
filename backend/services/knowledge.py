"""User-scoped editable memory; historical artifacts stay immutable."""
import json
from sqlalchemy import select
from database.models import UserKnowledge, UserMemory
from services.run_store import encode


def knowledge(db, uid):
    row = db.get(UserKnowledge, uid)
    if row is None:
        row = UserKnowledge(user_id=uid, revision=1, profile_json='{}')
        db.add(row)
        db.flush()
    return row


def memories(db, uid):
    return [json.loads(row.body_json) for row in db.scalars(select(UserMemory).where(
        UserMemory.user_id == uid, UserMemory.deleted == 0).order_by(UserMemory.revision, UserMemory.memory_id))]


def refresh(db, uid, state):
    account = knowledge(db, uid)
    if state.get('knowledge_revision') != account.revision:
        state['memories'] = memories(db, uid)
        if state.get('knowledge_revision') is not None:
            state['profile_version'] += 1
        if not state.get('profile') or state.get('uses_account_profile', True):
            state['profile'] = json.loads(account.profile_json)
        state['knowledge_revision'] = account.revision
    return state


def sync_memories(db, uid, state):
    account = knowledge(db, uid)
    changed = False
    for entry in state.get('memories', []):
        if not entry.get('confirmed'):
            continue
        existing = db.get(UserMemory, (uid, entry['id']))
        if existing is None:
            db.add(UserMemory(user_id=uid, memory_id=entry['id'], body_json=encode(entry),
                revision=account.revision + 1, deleted=0))
            changed = True
        # A tombstone or user correction always wins over old execution state.
    if changed:
        account.revision += 1
    state['knowledge_revision'] = account.revision


def edit_memory(db, uid, mid, *, value=None, delete=False):
    row = db.get(UserMemory, (uid, mid))
    if row is None or row.deleted:
        raise KeyError(mid)
    account = knowledge(db, uid)
    account.revision += 1
    row.revision = account.revision
    if delete:
        row.deleted = 1
        row.body_json = '{}'
    else:
        body = json.loads(row.body_json)
        if body['field'] == 'annual_budget_max' and (not value.isascii() or not value.isdigit()):
            raise ValueError('预算需要非负整数金额')
        body.update(value=value, quote=value, edited_by_user=True)
        row.body_json = encode(body)
    return account.revision
