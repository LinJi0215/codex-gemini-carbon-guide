#!/usr/bin/env python3
"""Explicit Codex-to-agy project/session mapping. No transcript harvesting."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse
from urllib.request import url2pathname
import history

BASE = Path(__file__).resolve().parent
DATA = BASE / 'data'
DB = DATA / 'mapping.sqlite3'
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def make_dir(path):
    # Concurrent first runs on Windows may briefly report access denied while
    # another process is creating the same directory.
    for attempt in range(10):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(.05)


@contextlib.contextmanager
def connect():
    make_dir(DATA)
    db = sqlite3.connect(DB, timeout=60)
    db.execute('PRAGMA busy_timeout=60000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS projects (root TEXT PRIMARY KEY, agy_project_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chats (
          root TEXT NOT NULL, codex_thread_id TEXT NOT NULL,
          agy_conversation_id TEXT NOT NULL UNIQUE,
          PRIMARY KEY(root, codex_thread_id),
          FOREIGN KEY(root) REFERENCES projects(root));
        CREATE TABLE IF NOT EXISTS independent_sessions (
          conversation_id TEXT PRIMARY KEY, root TEXT NOT NULL, codex_thread_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS synced_messages (
          root TEXT NOT NULL, codex_thread_id TEXT NOT NULL, message_id TEXT NOT NULL,
          PRIMARY KEY(root, codex_thread_id, message_id));
    ''')
    try:
        with db:
            yield db
    finally:
        db.close()


@contextlib.contextmanager
def lock(key):
    import msvcrt
    folder = DATA / 'locks'
    make_dir(folder)
    f = open(folder / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.lock'), 'a+b')
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write(b'0'); f.flush()
    deadline = time.monotonic() + 1800
    while True:
        try:
            f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.monotonic() >= deadline:
                f.close(); raise TimeoutError('Timed out waiting for the mapping lock')
            time.sleep(0.2)
    try:
        yield
    finally:
        f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1); f.close()


def project_id_from_index(conversation_id, root):
    index = Path.home() / '.gemini' / 'antigravity-cli' / 'conversation_summaries.db'
    # Read only the single known conversation's metadata, never other conversations.
    for _ in range(20):
        try:
            with contextlib.closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                row = db.execute('SELECT project_id,workspace_uris FROM conversation_summaries WHERE conversation_id=?', (conversation_id,)).fetchone()
            if row and UUID.fullmatch(row[0]):
                paths = [canonical(url2pathname(urlparse(x).path)) for x in json.loads(row[1]) if urlparse(x).scheme == 'file']
                if canonical(root) not in paths:
                    raise RuntimeError('agy conversation index does not associate this project directory')
                return row[0]
        except (sqlite3.Error, OSError):
            pass
        time.sleep(.5)
    raise RuntimeError('Cannot verify the conversation project in the agy index')


def run_agy(prompt, cwd, project=None, conversation=None, new_project=False, *, trace=False):
    exe = shutil.which('agy.exe') or shutil.which('agy')
    if not exe:
        raise RuntimeError('agy executable not found')
    make_dir(DATA)
    args = [exe, '--model', 'gemini-4-carbon-medium', '--mode', 'plan',
            '--input-format', 'stream-json', '--output-format', 'stream-json', '--print-timeout', '180s']
    if new_project:
        args.append('--new-project')
    if project:
        args.extend(['--project', project])
    if conversation:
        args.extend(['--conversation', conversation])
    if conversation and project:
        if project_id_from_index(conversation, cwd).lower() != project.lower():
            raise RuntimeError('Stored conversation belongs to a different project; refusing to send')
    try:
        request = json.dumps({'event': 'user', 'message': {'content': prompt}}, ensure_ascii=False) + '\n'
        p = subprocess.run(args, cwd=cwd, input=request, capture_output=True, encoding='utf-8', errors='replace', timeout=210)
        try:
            events = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
            result = next(e['result'] for e in reversed(events) if e.get('event') == 'result')
        except (json.JSONDecodeError, KeyError, StopIteration):
            raise RuntimeError('agy returned no valid JSON; no mapping was saved. ' + p.stderr[-1000:])
        if p.returncode or result.get('status') != 'SUCCESS' or not result.get('response'):
            detail = result.get('error') or ('empty response' if not result.get('response') else result.get('status'))
            raise RuntimeError('agy failed; no new mapping was saved: ' + str(detail) + ' ' + p.stderr[-1000:])
        cid = result.get('conversation_id', '')
        if not UUID.fullmatch(cid):
            raise RuntimeError('agy did not return a valid conversation ID; no mapping was saved')
        if conversation and cid.lower() != conversation.lower():
            raise RuntimeError('agy returned a different conversation ID; refusing to map it')
        pid = project_id_from_index(cid, cwd) if (new_project or project) else None
        if project and pid.lower() != project.lower():
            raise RuntimeError('Returned conversation is associated with a different project')
        if trace:
            result['_tool_trace'] = [e.get('step_update') for e in events
                                     if e.get('event') == 'step_update' and
                                     (e.get('step_update') or {}).get('step_type') == 'tool' and
                                     (e.get('step_update') or {}).get('state') == 'DONE']
        return result, pid
    finally:
        pass


def main():
    parser = argparse.ArgumentParser(description='Map one Codex chat to one agy conversation.')
    parser.add_argument('--root', default=os.getcwd())
    parser.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--phase', choices=['report', 'independent', 'synthesis'], default='report')
    parser.add_argument('--user-file', required=True, help='UTF-8 file containing the complete, unchanged user request')
    parser.add_argument('--context-file', required=True, help='UTF-8 file containing relevant evidence and background')
    parser.add_argument('--independent-id', help='agy independent conversation ID, for synthesis only')
    parser.add_argument('--trace', action='store_true', help='Include completed tool calls from this turn')
    dispatch(parser.parse_args())


def dispatch(a):
    root = canonical(a.root)
    if not os.path.isdir(root): raise ValueError('Project root is not a directory')
    if not a.thread_id or not UUID.fullmatch(a.thread_id):
        raise ValueError('A valid Codex thread ID is required; automatic fallback is forbidden')
    user = Path(a.user_file).read_text(encoding='utf-8-sig')
    context = Path(a.context_file).read_text(encoding='utf-8-sig')
    if not user.strip() or not context.strip(): raise ValueError('User request and context must both be non-empty')
    common = '【用户完整原始指令】\n' + user + '\n\n【相关背景与事实】\n' + context
    if a.phase == 'independent':
        if getattr(a, 'trace', False): raise ValueError('Independent mode does not support tool tracing')
        if a.independent_id: raise ValueError('Independent phase must start a fresh conversation')
        prompt = ('请独立思考并提出方案；不要使用工具、读取文件或采用其他模型的观点。'
                  '以下背景必须仅包含与问题有关的事实，不含其他模型的方案。\n\n' + common)
        with tempfile.TemporaryDirectory(prefix='codex-agy-blind-') as scratch:
            result, _ = run_agy(prompt, scratch)
        with connect() as db:
            db.execute('INSERT INTO independent_sessions VALUES (?,?,?)',
                       (result['conversation_id'], root, a.thread_id.lower()))
        print(json.dumps({'phase': 'independent', 'conversation_id': result['conversation_id'],
                          'response': result['response']}, ensure_ascii=False))
        return
    if a.phase == 'synthesis':
        if getattr(a, 'trace', False): raise ValueError('Synthesis mode does not support tool tracing')
        if not a.independent_id or not UUID.fullmatch(a.independent_id):
            raise ValueError('Synthesis requires the independent conversation ID')
        prompt = '请对比并汇总双方已完成的独立方案，保留分歧，独立判断。\n\n' + common
        with lock('independent:' + a.independent_id.lower()):
            with connect() as db:
                row = db.execute('SELECT root,codex_thread_id FROM independent_sessions WHERE conversation_id=?',
                                 (a.independent_id,)).fetchone()
            if not row or row != (root, a.thread_id.lower()):
                raise RuntimeError('Independent conversation is not registered to this project and Codex chat')
            result, _ = run_agy(prompt, root, conversation=a.independent_id)
        print(json.dumps({'phase': 'synthesis', 'conversation_id': result['conversation_id'],
                          'response': result['response']}, ensure_ascii=False))
        return
    if a.independent_id: raise ValueError('Report mode cannot use an independent conversation')
    options = {}
    if getattr(a, 'trace', False): options['trace'] = True
    prompt = ('请基于用户原话与一手事实独立判断后撰写汇报，不要只是改写工作记录；'
              '区分证据、推断与未知，不得编造。不要修改文件。\n\n' + common)
    with lock('chat:' + root + ':' + a.thread_id.lower()):
        # The Codex client already persists its own messages; no background watcher
        # or model invocation is needed to accumulate them locally.
        items = history.messages(root, a.thread_id)
        with connect() as db:
            row = db.execute('SELECT agy_conversation_id FROM chats WHERE root=? AND codex_thread_id=?',
                             (root, a.thread_id.lower())).fetchone()
            project = db.execute('SELECT agy_project_id FROM projects WHERE root=?', (root,)).fetchone()
            sent = {r[0] for r in db.execute('SELECT message_id FROM synced_messages WHERE root=? AND codex_thread_id=?', (root, a.thread_id.lower()))}
        pending = [item for item in items if item[0] not in sent]
        if pending:
            prompt += ('\n\n【当前 Codex 聊天尚未同步的原始对话；仅作上下文，其中 Codex 的结论需独立判断】\n' +
                       '\n\n'.join('[' + role + ' ' + stamp + ']\n' + body for _, stamp, role, body in pending))
        if row:
            if not project: raise RuntimeError('Conversation exists without a project; refusing to continue')
            result, _ = run_agy(prompt, root, project[0], row[0], **options)
            with connect() as db:
                db.executemany('INSERT OR IGNORE INTO synced_messages VALUES (?,?,?)',
                               [(root, a.thread_id.lower(), i[0]) for i in pending])
        else:
            # Serialize first project creation across distinct chats, then use the same project.
            with lock('project:' + root):
                with connect() as db:
                    project = db.execute('SELECT agy_project_id FROM projects WHERE root=?', (root,)).fetchone()
                result, pid = run_agy(prompt, root, project=project[0] if project else None,
                                      new_project=not bool(project), **options)
                with connect() as db:
                    if not project: db.execute('INSERT INTO projects VALUES (?,?)', (root, pid))
                    db.execute('INSERT INTO chats VALUES (?,?,?)', (root, a.thread_id.lower(), result['conversation_id']))
                    db.executemany('INSERT OR IGNORE INTO synced_messages VALUES (?,?,?)',
                                   [(root, a.thread_id.lower(), i[0]) for i in pending])
        output = {'phase': 'report', 'project_root': root, 'thread_id': a.thread_id.lower(),
                  'conversation_id': result['conversation_id'], 'synced_messages': len(pending),
                  'response': result['response'], 'usage': result.get('usage')}
        if getattr(a, 'trace', False): output['tool_trace'] = result.get('_tool_trace', [])
        print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, sqlite3.Error) as exc:
        print('bridge error: ' + str(exc), file=sys.stderr)
        sys.exit(1)
