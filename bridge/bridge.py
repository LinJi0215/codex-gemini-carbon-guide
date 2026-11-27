#!/usr/bin/env python3
"""Codex-to-agy mapping with on-demand current-chat synchronization."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
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


def scoped_read_settings(paths):
    """Retain existing permission rules while adding exact per-call read grants."""
    settings_path = Path.home() / '.gemini' / 'antigravity-cli' / 'settings.json'
    existing = {}
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
        existing = settings.get('permissions', {})
        if not isinstance(existing, dict): raise ValueError('Invalid agy permissions settings')
    permissions = dict(existing)
    allowed = permissions.get('allow', [])
    if not isinstance(allowed, list): raise ValueError('Invalid agy allow rules')
    rules = list(allowed)
    for path in paths:
        if os.path.isfile(path) and '*' not in path:
            rule = 'read_file(' + path + ')'
            if rule not in rules: rules.append(rule)
    permissions['allow'] = rules
    return {'permissions': permissions}


def agy_status(conversation_id):
    """Read one known conversation's status; it may be stale or unavailable."""
    if not conversation_id or not UUID.fullmatch(conversation_id): return 'unknown'
    index = Path.home() / '.gemini' / 'antigravity-cli' / 'conversation_summaries.db'
    try:
        with contextlib.closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            row = db.execute('SELECT status,not_fully_idle FROM conversation_summaries WHERE conversation_id=?', (conversation_id,)).fetchone()
        if row and row[0] == 'CASCADE_RUN_STATUS_RUNNING' and row[1]: return 'index_reports_running'
        if row and row[0] == 'CASCADE_RUN_STATUS_IDLE' and not row[1]: return 'index_reports_idle'
    except (sqlite3.Error, OSError):
        pass
    return 'unknown'


def capture_stream(args, cwd, request, *, timeout_seconds=0, progress_interval=180, timeout_grace=30):
    """Drain both pipes while waiting for a terminal result; never retry a turn."""
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1)
    incoming = queue.Queue()
    def reader(label, stream):
        try:
            for line in stream: incoming.put((label, line))
        finally:
            stream.close()
            incoming.put((label, None))
    def writer():
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError) as exc:
            incoming.put(('writer_error', str(exc)))
            try: proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError): pass
    workers = []
    for target, arguments in ((reader, ('out', proc.stdout)), (reader, ('err', proc.stderr)), (writer, ())):
        worker = threading.Thread(target=target, args=arguments, daemon=True)
        worker.start()
        workers.append(worker)
    def stop_process():
        proc.kill()
        proc.wait()
        for worker in workers: worker.join(timeout=2)
    events, stderr_parts = [], []
    closed = set()
    cid = None
    start = time.monotonic()
    next_progress = start + progress_interval
    last_activity = start
    while len(closed) < 2 or proc.poll() is None:
        now = time.monotonic()
        if timeout_seconds and now - start >= timeout_seconds + timeout_grace:
            stop_process()
            raise RuntimeError('agy client timed out; the remote turn may still be running. No success was recorded; do not automatically retry.')
        if now >= next_progress:
            status = agy_status(cid)
            print(f'agy waiting: elapsed={int(now-start)}s last_event={int(now-last_activity)}s ago status={status}; status may be stale, continuing to wait.', file=sys.stderr, flush=True)
            next_progress = now + progress_interval
        try:
            label, line = incoming.get(timeout=min(1.0, max(.01, next_progress-now)))
        except queue.Empty:
            continue
        if line is None:
            closed.add(label)
        elif label == 'out':
            if not line.strip(): continue
            try: event = json.loads(line)
            except json.JSONDecodeError:
                stop_process()
                raise RuntimeError('agy returned invalid JSON; no mapping was saved')
            events.append(event)
            last_activity = time.monotonic()
            if event.get('event') == 'init': cid = event.get('conversation_id')
        elif label == 'err':
            stderr_parts.append(line)
        else:
            stderr_parts.append('stdin write failed: ' + line)
    return events, ''.join(stderr_parts)[-8000:], proc.wait()


def run_agy(prompt, cwd, project=None, conversation=None, new_project=False, *, trace=False, read_paths=None, timeout_seconds=0):
    exe = shutil.which('agy.exe') or shutil.which('agy')
    if not exe:
        raise RuntimeError('agy executable not found')
    make_dir(DATA)
    args = [exe, '--model', 'gemini-4-carbon-medium', '--mode', 'plan',
            '--input-format', 'stream-json', '--output-format', 'stream-json', '--print-timeout', str(timeout_seconds) + 's']
    if read_paths:
        # Each --file is an explicit, per-invocation request. Never grant a
        # directory, wildcard, writes or commands. Existing deny/ask still apply.
        settings = scoped_read_settings(read_paths)
        args.extend(['--settings', json.dumps(settings, ensure_ascii=False)])
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
        events, stderr, returncode = capture_stream(args, cwd, request, timeout_seconds=timeout_seconds)
        try:
            result = next(e['result'] for e in reversed(events) if e.get('event') == 'result')
        except (KeyError, StopIteration):
            raise RuntimeError('agy returned no valid result; no mapping was saved. ' + stderr[-1000:])
        if returncode or result.get('status') != 'SUCCESS' or not result.get('response'):
            detail = result.get('error') or ('empty response' if not result.get('response') else result.get('status'))
            if 'print_timeout' in stderr or 'print timeout' in stderr:
                raise RuntimeError('agy print timeout; the turn may still be in progress. No success was recorded; do not automatically retry. ' + stderr[-1000:])
            raise RuntimeError('agy failed; no new mapping was saved: ' + str(detail) + ' ' + stderr[-1000:])
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


def file_statuses(paths, trace, root):
    """Match completed read tool calls to requested paths; never trust prose alone."""
    reads = {'read_file', 'view_file', 'view_image', 'read_image'}
    result = []
    for path in paths:
        status = 'not_confirmed'
        try:
            metadata = os.stat(path)
            if not stat.S_ISREG(metadata.st_mode): status = 'not_a_file'
        except FileNotFoundError:
            status = 'not_found'
        except PermissionError:
            status = 'local_permission_denied'
        except OSError:
            status = 'local_error'
        for step in trace:
            info = step.get('tool_info') or {}
            name = info.get('name') or step.get('tool_name') or ''
            if name not in reads: continue
            params = info.get('parameters') or {}
            if isinstance(params, str):
                try: params = json.loads(params)
                except ValueError: continue
            if not isinstance(params, dict): continue
            target = next((v for k, v in params.items() if k.lower() in ('path', 'file_path', 'filepath', 'absolute_path') and isinstance(v, str)), None)
            if not target: continue
            target = target if os.path.isabs(target) else os.path.join(root, target)
            if canonical(target) != canonical(path): continue
            detail = str(info.get('error') or info.get('output') or '').lower()
            if info.get('error') or any(x in detail for x in ('permission denied', 'access denied', 'not allowed', '拒绝访问', '权限不足', 'file not found', 'no such file', 'unsupported format', 'error reading', 'failed to read')):
                status = 'read_failed'
            elif info.get('output'):
                status = 'read_tool_succeeded'
            elif status == 'not_confirmed':
                status = 'read_tool_completed_unconfirmed'
        result.append({'path': path, 'status': status})
    return result


def main():
    parser = argparse.ArgumentParser(description='Map one Codex chat to one agy conversation.')
    parser.add_argument('--root', default=os.getcwd())
    parser.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--phase', choices=['report', 'independent', 'synthesis'], default='report')
    parser.add_argument('--user-file', required=True, help='UTF-8 file containing the complete, unchanged user request')
    parser.add_argument('--context-file', required=True, help='UTF-8 file containing relevant evidence and background')
    parser.add_argument('--independent-id', help='agy independent conversation ID, for synthesis only')
    parser.add_argument('--trace', action='store_true', help='Include completed tool calls from this turn')
    parser.add_argument('--file', action='append', default=[], help='Relevant local file to ask Gemini to read; repeatable')
    parser.add_argument('--chat-image-message', action='append', default=[], help='Export images from this current-chat user message ID for this call; repeatable')
    parser.add_argument('--timeout-seconds', type=int, default=0, help='Optional explicit maximum wait for agy; default 0 waits until completion')
    dispatch(parser.parse_args())


def dispatch(a):
    root = canonical(a.root)
    if not os.path.isdir(root): raise ValueError('Project root is not a directory')
    if not a.thread_id or not UUID.fullmatch(a.thread_id):
        raise ValueError('A valid Codex thread ID is required; automatic fallback is forbidden')
    if getattr(a, 'timeout_seconds', 0) != 0 and not 30 <= a.timeout_seconds <= 3600: raise ValueError('Timeout must be 0 or between 30 and 3600 seconds')
    user = Path(a.user_file).read_text(encoding='utf-8-sig')
    context = Path(a.context_file).read_text(encoding='utf-8-sig')
    if not user.strip() or not context.strip(): raise ValueError('User request and context must both be non-empty')
    common = '【用户完整原始指令】\n' + user + '\n\n【相关背景与事实】\n' + context
    if a.phase == 'independent':
        if getattr(a, 'file', []) or getattr(a, 'chat_image_message', []): raise ValueError('Independent mode does not accept files or chat images')
        if getattr(a, 'trace', False): raise ValueError('Independent mode does not support tool tracing')
        if a.independent_id: raise ValueError('Independent phase must start a fresh conversation')
        prompt = ('请独立思考并提出方案；不要使用工具、读取文件或采用其他模型的观点。'
                  '以下背景必须仅包含与问题有关的事实，不含其他模型的方案。\n\n' + common)
        options = {} if getattr(a, 'timeout_seconds', 0) == 0 else {'timeout_seconds': a.timeout_seconds}
        with tempfile.TemporaryDirectory(prefix='codex-agy-blind-') as scratch:
            result, _ = run_agy(prompt, scratch, **options)
        with connect() as db:
            db.execute('INSERT INTO independent_sessions VALUES (?,?,?)',
                       (result['conversation_id'], root, a.thread_id.lower()))
        print(json.dumps({'phase': 'independent', 'conversation_id': result['conversation_id'],
                          'response': result['response']}, ensure_ascii=False))
        return
    if a.phase == 'synthesis':
        if getattr(a, 'file', []) or getattr(a, 'chat_image_message', []): raise ValueError('Synthesis mode does not accept files or chat images')
        if getattr(a, 'trace', False): raise ValueError('Synthesis mode does not support tool tracing')
        if not a.independent_id or not UUID.fullmatch(a.independent_id):
            raise ValueError('Synthesis requires the independent conversation ID')
        prompt = '请对比并汇总双方已完成的独立方案，保留分歧，独立判断。\n\n' + common
        options = {} if getattr(a, 'timeout_seconds', 0) == 0 else {'timeout_seconds': a.timeout_seconds}
        with lock('independent:' + a.independent_id.lower()):
            with connect() as db:
                row = db.execute('SELECT root,codex_thread_id FROM independent_sessions WHERE conversation_id=?',
                                 (a.independent_id,)).fetchone()
            if not row or row != (root, a.thread_id.lower()):
                raise RuntimeError('Independent conversation is not registered to this project and Codex chat')
            result, _ = run_agy(prompt, root, conversation=a.independent_id, **options)
        print(json.dumps({'phase': 'synthesis', 'conversation_id': result['conversation_id'],
                          'response': result['response']}, ensure_ascii=False))
        return
    if a.independent_id: raise ValueError('Report mode cannot use an independent conversation')
    with contextlib.ExitStack() as stack:
        image_dir = None
        if getattr(a, 'chat_image_message', []):
            image_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix='.codex-agy-images-', dir=root))
        report(a, root, common, image_dir)


def report(a, root, common, image_dir):
    options = {}
    paths = []
    for raw in getattr(a, 'file', []):
        path = canonical(raw if os.path.isabs(raw) else os.path.join(root, raw))
        if path not in paths: paths.append(path)
    prompt = ('请基于用户原话与一手事实独立判断后撰写汇报，不要只是改写工作记录；'
              '区分证据、推断与未知，不得编造。带有未同步附件标记的消息中，附件内容未提供，'
              '不得声称看过，必要时请 Codex 补充。不要修改文件。\n\n' + common)
    with lock('chat:' + root + ':' + a.thread_id.lower()):
        # The Codex client already persists its own messages; no background watcher
        # or model invocation is needed to accumulate them locally.
        if image_dir:
            items = history.messages(root, a.thread_id, image_messages=a.chat_image_message, image_dir=image_dir)
            paths.extend(str(x) for x in sorted(Path(image_dir).iterdir()))
        else:
            items = history.messages(root, a.thread_id)
        if paths:
            options['trace'] = True
            options['read_paths'] = paths
            prompt += ('\n\n【本次需要读取的本地文件】\n' + '\n'.join(paths) +
                '\n请逐项尝试使用读取工具打开文件，说明成功或失败及原因。图片须实际使用能理解图像的工具，'
                '不得仅凭路径或文件名声称看过内容。临时图片对应本次指定的当前聊天附件。')
        if getattr(a, 'trace', False): options['trace'] = True
        if getattr(a, 'timeout_seconds', 0) != 0: options['timeout_seconds'] = a.timeout_seconds
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
                  'omitted_attachment_markers': sum(item[3].count('[未同步的非文本附件: ') for item in pending),
                  'response': result['response'], 'usage': result.get('usage')}
        if paths: output['file_status'] = file_statuses(paths, result.get('_tool_trace', []), root)
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
