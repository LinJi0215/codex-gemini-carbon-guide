import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import bridge


class BridgeTests(unittest.TestCase):
    def test_concurrent_chats_and_resume_are_isolated(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            (base / 'user.txt').write_text('original question', encoding='utf-8')
            (base / 'context.txt').write_text('facts', encoding='utf-8')
            created_projects = []
            calls = []
            def fake(prompt, cwd, project=None, conversation=None, new_project=False):
                calls.append((project, conversation, new_project))
                time.sleep(.01)
                if new_project: created_projects.append(1)
                return {'conversation_id': conversation or str(uuid.uuid4()), 'response': 'ok'}, project or '00000000-0000-0000-0000-000000000010'
            def run(thread):
                args = argparse.Namespace(root=str(base), thread_id=thread, phase='report',
                    user_file=str(base / 'user.txt'), context_file=str(base / 'context.txt'), independent_id=None)
                bridge.dispatch(args)
            ids = [str(uuid.uuid4()) for _ in range(6)]
            with patch.object(bridge, 'DATA', base / 'data'), patch.object(bridge, 'DB', base / 'data' / 'mapping.sqlite3'), patch.object(bridge.history, 'messages', return_value=[]), patch.object(bridge, 'run_agy', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(executor.map(run, ids + [ids[0], ids[1]]))
                with bridge.connect() as db:
                    rows = db.execute('SELECT codex_thread_id,agy_conversation_id FROM chats').fetchall()
                self.assertEqual(len(created_projects), 1)
                self.assertEqual(len(rows), 6)
                self.assertEqual(len({r[1] for r in rows}), 6)
                self.assertEqual(sum(x[1] is not None for x in calls), 2)

    def test_independent_does_not_resume_or_access_project(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            (base / 'u').write_text('question', encoding='utf-8')
            (base / 'c').write_text('facts', encoding='utf-8')
            args = argparse.Namespace(root=str(base), thread_id=str(uuid.uuid4()), phase='independent',
                                      user_file=str(base / 'u'), context_file=str(base / 'c'), independent_id=None)
            with patch.object(bridge.tempfile, 'tempdir', str(base)), patch.object(bridge, 'DATA', base / 'data'), patch.object(bridge, 'DB', base / 'data' / 'mapping.sqlite3'), patch.object(bridge, 'run_agy', return_value=({'conversation_id': str(uuid.uuid4()), 'response':'ok'}, None)) as call, contextlib.redirect_stdout(io.StringIO()):
                bridge.dispatch(args)
            self.assertEqual(len(call.call_args.args), 2)
            self.assertEqual(call.call_args.kwargs, {})
            self.assertNotEqual(os.path.normcase(call.call_args.args[1]), bridge.canonical(base))

    def test_synthesis_cannot_use_unregistered_session(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            (base / 'u').write_text('question', encoding='utf-8')
            (base / 'c').write_text('facts', encoding='utf-8')
            args = argparse.Namespace(root=str(base), thread_id=str(uuid.uuid4()), phase='synthesis',
                                      user_file=str(base / 'u'), context_file=str(base / 'c'), independent_id=str(uuid.uuid4()))
            with patch.object(bridge, 'DATA', base / 'data'), patch.object(bridge, 'DB', base / 'data' / 'mapping.sqlite3'), patch.object(bridge, 'run_agy') as call:
                with self.assertRaises(RuntimeError): bridge.dispatch(args)
                call.assert_not_called()

    def test_incremental_sync_only_marks_successful_delivery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            (base / 'u').write_text('question', encoding='utf-8')
            (base / 'c').write_text('facts', encoding='utf-8')
            args = argparse.Namespace(root=str(base), thread_id=str(uuid.uuid4()), phase='report',
                user_file=str(base / 'u'), context_file=str(base / 'c'), independent_id=None)
            items = [('m1','2026-01-01','user','first'), ('m2','2026-01-02','assistant','answer')]
            calls = []
            cid = str(uuid.uuid4()); pid = str(uuid.uuid4())
            def fake(prompt, cwd, project=None, conversation=None, new_project=False):
                calls.append(prompt)
                if len(calls) == 2: raise RuntimeError('simulated failure')
                return {'conversation_id': cid, 'response': 'ok'}, pid
            with patch.object(bridge, 'DATA', base/'data'), patch.object(bridge, 'DB', base/'data'/'mapping.sqlite3'), patch.object(bridge.history, 'messages', side_effect=lambda *_: list(items)), patch.object(bridge, 'run_agy', side_effect=fake), contextlib.redirect_stdout(io.StringIO()):
                bridge.dispatch(args)
                items.append(('m3','2026-01-03','user','new'))
                with self.assertRaises(RuntimeError): bridge.dispatch(args)
                bridge.dispatch(args)
                self.assertIn('first', calls[0]); self.assertIn('new', calls[1])
                self.assertIn('new', calls[2]); self.assertNotIn('first', calls[2])
                with bridge.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM synced_messages').fetchone()[0], 3)


if __name__ == '__main__': unittest.main()
