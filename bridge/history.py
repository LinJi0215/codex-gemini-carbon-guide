"""Read the current Codex chat's already persisted user/final messages on demand.

The rollout format is an implementation detail: validate identity and fail closed.
Never scan the content of other chats. No model calls or background process here.
"""
import json
import os
from pathlib import Path
import re


ID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def messages(root, thread_id, sessions_dir=None):
    current = os.environ.get('CODEX_THREAD_ID', '')
    if not ID.fullmatch(thread_id) or thread_id.lower() != current.lower():
        raise ValueError('History can only be read for the current Codex chat')
    base = Path(sessions_dir) if sessions_dir is not None else Path.home() / '.codex' / 'sessions'
    # File names may end in the thread UUID or in thread_UUID + segment UUID.
    pattern = re.compile(r'-' + re.escape(thread_id) + r'(?:_' + ID.pattern[1:-1] + r')?\.jsonl$', re.I)
    candidates = sorted((p for p in base.rglob('*.jsonl') if pattern.search(p.name)), key=lambda p: p.name)
    if not candidates:
        raise RuntimeError('No local history found for the current chat')
    found = {}
    last_meta_cwd = None
    last_turn_cwd = None
    for path in candidates:
        with path.open('r', encoding='utf-8') as f:
            first = json.loads(f.readline())
            meta = first.get('payload', {})
            if (first.get('type') != 'session_meta' or str(meta.get('id','')).lower() != thread_id.lower()
                or not meta.get('cwd') or meta.get('thread_source') not in ('user', 'agent_created_thread')):
                raise RuntimeError('Session identity or directory is inconsistent; history sync stopped')
            last_meta_cwd = meta['cwd']
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    # A partially-written final line cannot be safely included yet.
                    if line.endswith('\n'): raise RuntimeError('Invalid history record; sync stopped')
                    continue
                p = item.get('payload') or {}
                if item.get('type') == 'turn_context' and p.get('cwd'):
                    last_turn_cwd = p['cwd']
                if item.get('type') != 'response_item' or p.get('type') != 'message': continue
                role = p.get('role')
                if role != 'user' and not (role == 'assistant' and p.get('phase') == 'final_answer'): continue
                mid = p.get('id')
                if not isinstance(mid, str) or not mid: raise RuntimeError('History message lacks stable ID')
                blocks = p.get('content') or []
                text = []
                for block in blocks:
                    if block.get('type') in ('input_text', 'output_text'):
                        text.append(block.get('text', ''))
                    else:
                        kind = str(block.get('type') or 'unknown')
                        text.append('[未同步的非文本附件: ' + kind + '；如需要其内容，请由 Codex 补充，不能假装已读取。]')
                text = '\n'.join(text)
                if role == 'user' and (text.startswith('# AGENTS.md instructions for ') or
                                       text.lstrip().startswith('<environment_context>')):
                    continue
                if not text.strip(): continue
                value = (item.get('timestamp', ''), role, text)
                if mid in found and found[mid] != value: raise RuntimeError('Conflicting duplicate history ID')
                found[mid] = value
    # A user-owned chat may have been moved between projects. session_meta.cwd
    # describes its original location, while the latest turn_context.cwd records
    # where the current turn runs. Keep thread identity and source checks intact.
    current_cwd = last_turn_cwd or last_meta_cwd
    try:
        in_root = bool(current_cwd) and os.path.commonpath([canonical(current_cwd), canonical(root)]) == canonical(root)
    except ValueError:
        in_root = False
    if not in_root:
        raise RuntimeError('Session identity or directory is inconsistent; history sync stopped')
    return [(mid, *found[mid]) for mid in sorted(found, key=lambda i: (found[i][0], i))]
