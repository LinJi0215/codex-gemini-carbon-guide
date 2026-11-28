import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import unittest
from unittest.mock import patch
import uuid
import bridge


class BridgeTests(unittest.TestCase):
    def test_vetted_directory_grants_and_read_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); share = base/'share'; share.mkdir()
            inside = share/'a.txt'; outside = base/'other.txt'
            inside.write_text('inside'); outside.write_text('outside')
            path = bridge.validate_read_directory(str(share), str(base))
            settings = bridge.scoped_read_settings([], [path])
            self.assertIn('read_file('+path+')', settings['permissions']['allow'])
            trace = [{'tool_name':'read_file','tool_info':{'parameters':{'Path':str(f)},'output':'1 line'}} for f in (inside,outside)]
            status = bridge.directory_statuses([path], trace, str(base))
            self.assertEqual(len(status[0]['file_status']), 1)
            self.assertEqual(status[0]['file_status'][0]['path'], bridge.canonical(inside))
            self.assertEqual(status[0]['file_status'][0]['status'], 'read_tool_succeeded')
            self.assertEqual(bridge.directory_statuses([path], [], str(base))[0]['file_status'], [])
            with self.assertRaises(ValueError): bridge.validate_read_directory(str(Path.home()), str(base))

    def test_independent_rejects_read_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); (base/'u').write_text('q'); (base/'c').write_text('facts')
            args = argparse.Namespace(root=str(base),thread_id=str(uuid.uuid4()),phase='independent',
                user_file=str(base/'u'),context_file=str(base/'c'),independent_id=None,read_dir=[str(base)])
            with self.assertRaisesRegex(ValueError, 'Independent mode'): bridge.dispatch(args)

    def test_report_passes_directory_grant_and_reports_file_reads(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); share = base/'share'; share.mkdir()
            (base/'u').write_text('question'); (base/'c').write_text('facts')
            f = share/'record.txt'; f.write_text('record')
            cid = str(uuid.uuid4()); pid = str(uuid.uuid4())
            args = argparse.Namespace(root=str(base), thread_id=str(uuid.uuid4()), phase='report',
                user_file=str(base/'u'),context_file=str(base/'c'),independent_id=None,read_dir=[str(share)])
            trace = [{'tool_name':'read_file','tool_info':{'parameters':{'Path':str(f)},'output':'1 line'}}]
            output = io.StringIO()
            with patch.object(bridge, 'DATA', base/'data'), patch.object(bridge, 'DB', base/'data'/'mapping.sqlite3'), patch.object(bridge.history, 'messages', return_value=[]), patch.object(bridge, 'run_agy', return_value=({'conversation_id':cid,'response':'ok','_tool_trace':trace},pid)) as call, contextlib.redirect_stdout(output):
                bridge.dispatch(args)
            self.assertEqual(call.call_args.kwargs['read_dirs'], [bridge.canonical(share)])
            self.assertEqual(json.loads(output.getvalue())['directory_status'][0]['file_status'][0]['status'], 'read_tool_succeeded')

    def test_per_call_read_scope_and_timeout_are_explicit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); f = base/'source.txt'; f.write_text('hello')
            cid = str(uuid.uuid4())
            result = {'event':'result','result':{'status':'SUCCESS','response':'ok','conversation_id':cid}}
            with patch.object(bridge.shutil, 'which', return_value='agy.exe'), patch.object(bridge, 'DATA', base/'data'), patch.object(bridge, 'capture_stream', return_value=([result], '', 0)) as run:
                bridge.run_agy('question',str(base),read_paths=[str(f)],timeout_seconds=600)
                args = run.call_args.args[0]
                permissions = json.loads(args[args.index('--settings')+1])['permissions']
                self.assertEqual(permissions, {'allow':['read_file('+str(f)+')']})
                self.assertEqual(args[args.index('--print-timeout')+1], '600s')
                self.assertEqual(run.call_args.kwargs['timeout_seconds'], 600)

    def test_timeout_reports_unknown_remote_state_without_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            script = 'import time; time.sleep(3)'
            with self.assertRaisesRegex(RuntimeError, 'may still be running'):
                bridge.capture_stream([sys.executable, '-u', '-c', script], str(base), '', timeout_seconds=.1, timeout_grace=0, progress_interval=2)

    def test_progress_check_keeps_waiting_for_result(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); output = io.StringIO()
            script = 'import json,time; print(json.dumps({"event":"init"}),flush=True); time.sleep(.35); print(json.dumps({"event":"result","result":{"status":"SUCCESS"}}),flush=True)'
            with patch.object(bridge, 'agy_status', return_value='unknown'), contextlib.redirect_stderr(output):
                events, _, code = bridge.capture_stream([sys.executable, '-u', '-c', script], str(base), '', progress_interval=.05)
            self.assertEqual(code, 0)
            self.assertEqual(events[-1]['result']['status'], 'SUCCESS')
            self.assertIn('status=unknown', output.getvalue())
            self.assertIn('continuing to wait', output.getvalue())

    def test_scoped_permissions_preserve_existing_ask_and_deny(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory); f = base/'source.txt'; f.write_text('hello')
            cfg = base/'.gemini'/'antigravity-cli'; cfg.mkdir(parents=True)
            (cfg/'settings.json').write_text(json.dumps({'permissions': {'allow':['read_file(existing)'], 'ask':['read_file(ask)'], 'deny':['read_file(denied)']}}))
            with patch.object(bridge.Path, 'home', return_value=base): settings = bridge.scoped_read_settings([str(f)])
            self.assertEqual(settings['permissions']['ask'], ['read_file(ask)'])
            self.assertEqual(settings['permissions']['deny'], ['read_file(denied)'])
            self.assertEqual(settings['permissions']['allow'], ['read_file(existing)', 'read_file('+str(f)+')'])

    def test_file_status_requires_matching_successful_read_tool(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            first = base/'first.txt'; second = base/'second.txt'
            first.write_text('test'); second.write_text('test')
            trace = [
                {'tool_name':'read_file','tool_info':{'parameters':{'Path':str(first)},'output':'1 lines, 4 bytes'}},
                {'tool_name':'read_file','tool_info':{'parameters':{'Path':str(second)},'error':'permission denied'}},
                {'tool_name':'list_files','tool_info':{'parameters':{'Path':str(second)},'output':'found'}},
            ]
            statuses = bridge.file_statuses([str(first), str(second), str(base/'missing')], trace, str(base))
            self.assertEqual([x['status'] for x in statuses], ['read_tool_succeeded','read_failed','not_found'])
            self.assertEqual(bridge.file_statuses([str(first)], [], str(base))[0]['status'], 'not_confirmed')
            no_output = [{'tool_name':'read_file','tool_info':{'parameters':{'Path':str(first)}}}]
            self.assertEqual(bridge.file_statuses([str(first)], no_output, str(base))[0]['status'], 'read_tool_completed_unconfirmed')

    def test_independent_rejects_attachments(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base = Path(directory)
            (base/'u').write_text('q'); (base/'c').write_text('facts')
            args = argparse.Namespace(root=str(base), thread_id=str(uuid.uuid4()), phase='independent',
                user_file=str(base/'u'), context_file=str(base/'c'), independent_id=None, file=['x'])
            with self.assertRaises(ValueError): bridge.dispatch(args)

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
