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
    for path in candidates:
        with path.open('r', encoding='utf-8') as f:
            first = json.loads(f.readline())
            meta = first.get('payload', {})
            try:
                in_root = bool(meta.get('cwd')) and os.path.commonpath([canonical(meta['cwd']), canonical(root)]) == canonical(root)
            except ValueError:
                in_root = False
            if (first.get('type') != 'session_meta' or str(meta.get('id','')).lower() != thread_id.lower()
                or not in_root or meta.get('thread_source') != 'user'):
                raise RuntimeError('Session identity or directory is inconsistent; history sync stopped')
            for line in f:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    # A partially-written final line cannot be safely included yet.
                    if line.endswith('\n'): raise RuntimeError('Invalid history record; sync stopped')
                    continue
                p = item.get('payload') or {}
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
                        raise RuntimeError('Chat contains a non-text attachment; provide its context explicitly')
                text = '\n'.join(text)
                if role == 'user' and (text.startswith('# AGENTS.md instructions for ') or
                                       text.lstrip().startswith('<environment_context>')):
                    continue
                if not text.strip(): continue
                value = (item.get('timestamp', ''), role, text)
                if mid in found and found[mid] != value: raise RuntimeError('Conflicting duplicate history ID')
                found[mid] = value
    return [(mid, *found[mid]) for mid in sorted(found, key=lambda i: (found[i][0], i))]
