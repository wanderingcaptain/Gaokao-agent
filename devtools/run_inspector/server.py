"""Serve a standalone, read-only browser for the project's local Agent runs."""

import argparse
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urlsplit


HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE.parents[1] / 'backend' / 'database' / 'gaokao.db'
PAGE_SIZE = 25


def decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def database(path):
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def list_runs(conn, params, tables):
    status = params.get('status', [''])[0]
    query = params.get('q', [''])[0].strip()
    page = int(params.get('page', ['1'])[0])
    if not 1 <= page <= 1_000_000 or len(query) > 256:
        raise ValueError('无效的页码或 ID。')

    conditions, values = [], []
    if status:
        conditions.append('status = ?')
        values.append(status)
    if query:
        conditions.append('(substr(id, 1, length(?)) = ? OR substr(session_id, 1, length(?)) = ?)')
        values.extend([query, query, query, query])
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    total = conn.execute(f'SELECT count(*) FROM agent_runs_v2 {where}', values).fetchone()[0]
    page = min(page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    runs = [dict(row) for row in conn.execute(f'''
        SELECT id, session_id, status, attempts, created_at, updated_at
        FROM agent_runs_v2 {where}
        ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
    ''', [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE])]

    if runs and 'run_steps_v2' in tables:
        ids = [run['id'] for run in runs]
        placeholders = ','.join('?' for _ in ids)
        counts = {row['run_id']: dict(row) for row in conn.execute(f'''
            SELECT run_id, sum(kind = 'model') AS model_calls, sum(kind = 'tool') AS tool_calls,
                   sum(ok = 0) AS failed_steps
            FROM run_steps_v2 WHERE run_id IN ({placeholders}) GROUP BY run_id
        ''', ids)}
        for run in runs:
            for key in ('model_calls', 'tool_calls', 'failed_steps'):
                run[key] = counts.get(run['id'], {}).get(key, 0)

    statuses = [row[0] for row in conn.execute('SELECT DISTINCT status FROM agent_runs_v2 ORDER BY status')]
    return {'runs': runs, 'total': total, 'page': page, 'page_size': PAGE_SIZE, 'statuses': statuses,
            'as_of': time.time()}


def run_detail(conn, run_id, tables):
    row = conn.execute('SELECT * FROM agent_runs_v2 WHERE id = ?', (run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    for key in ('input', 'result', 'checkpoint'):
        run[key] = decode(run.pop(key + '_json'))

    steps = []
    if 'run_steps_v2' in tables:
        for row in conn.execute('SELECT * FROM run_steps_v2 WHERE run_id = ? ORDER BY rowid', (run_id,)):
            step = dict(row)
            step['usage'] = decode(step.pop('usage_json'))
            steps.append(step)

    events = []
    if 'run_events_v2' in tables:
        for row in conn.execute('SELECT * FROM run_events_v2 WHERE run_id = ? ORDER BY id', (run_id,)):
            event = dict(row)
            event['data'] = decode(event.pop('data_json'))
            events.append(event)

    return {'run': run, 'steps': steps, 'events': events, 'as_of': time.time()}


class InspectorHandler(BaseHTTPRequestHandler):
    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; connect-src 'self'; "
                         "script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        self.send_body(status, json.dumps(payload, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        port = self.server.server_port
        if self.headers.get('Host') not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
            self.send_json(HTTPStatus.FORBIDDEN, {'error': '请通过本机地址打开工具。'})
            return
        url = urlsplit(self.path)
        if url.path == '/':
            self.send_body(HTTPStatus.OK, self.server.page, 'text/html; charset=utf-8')
            return
        if url.path == '/api/runs' or url.path.startswith('/api/runs/'):
            try:
                with closing(database(self.server.db_path)) as conn:
                    conn.execute('BEGIN')
                    tables = table_names(conn)
                    if url.path == '/api/runs':
                        payload = list_runs(conn, parse_qs(url.query), tables)
                    else:
                        payload = run_detail(conn, unquote(url.path.removeprefix('/api/runs/')), tables)
                if payload is not None:
                    self.send_json(HTTPStatus.OK, payload)
                    return
            except ValueError:
                self.send_json(HTTPStatus.BAD_REQUEST, {'error': '无效的筛选参数。'})
                return
            except sqlite3.Error as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': f'读取数据库失败：{exc}'})
                return
        self.send_json(HTTPStatus.NOT_FOUND, {'error': '没有找到此记录或页面。'})


def main():
    parser = argparse.ArgumentParser(description='在浏览器中只读检查 Multi-Agent 历史执行记录。')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='SQLite 数据库文件路径')
    parser.add_argument('--port', type=int, default=8765, help='本机端口，默认 8765')
    args = parser.parse_args()
    db_path = args.db.expanduser().resolve()
    if not 1 <= args.port <= 65535:
        parser.error('端口须在 1–65535 之间。')
    if not db_path.is_file():
        parser.error(f'找不到数据库：{db_path}')
    try:
        with closing(database(db_path)) as conn:
            if 'agent_runs_v2' not in table_names(conn):
                parser.error(f'数据库没有 agent_runs_v2 表：{db_path}')
        page = (HERE / 'index.html').read_bytes()
        with ThreadingHTTPServer(('127.0.0.1', args.port), InspectorHandler) as server:
            server.db_path = db_path
            server.page = page
            print(f'执行记录检查器 http://127.0.0.1:{server.server_port}\n只读数据库 {db_path}', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f'启动失败：{exc}\n')


if __name__ == '__main__':
    main()
