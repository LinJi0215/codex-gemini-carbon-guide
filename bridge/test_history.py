import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import history


class HistoryTests(unittest.TestCase):
    def test_only_current_identity_and_user_facing_messages(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            tid = str(uuid.uuid4())
            path = root / ('rollout-2026-01-01T00-00-00-' + tid + '.jsonl')
            def entry(typ, payload, stamp='2026-01-01T00:00:00Z'):
                return json.dumps({'type':typ,'timestamp':stamp,'payload':payload})+'\n'
            text = entry('session_meta', {'id':tid,'cwd':str(root),'thread_source':'user'})
            text += entry('response_item', {'type':'message','id':'1','role':'user','content':[{'type':'input_text','text':'hello'}]})
            text += entry('response_item', {'type':'message','id':'2','role':'user','content':[{'type':'input_text','text':'<environment_context> secret'}]})
            text += entry('response_item', {'type':'message','id':'3','role':'assistant','phase':'analysis','content':[{'type':'output_text','text':'private'}]})
            text += entry('response_item', {'type':'message','id':'4','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'world'}]})
            text += entry('response_item', {'type':'message','id':'5','role':'user','content':[{'type':'input_text','text':'see image'},{'type':'input_image','image_url':'secret-not-to-copy'}]})
            path.write_text(text, encoding='utf-8')
            with patch.dict(os.environ, {'CODEX_THREAD_ID':tid}):
                messages = history.messages(str(root),tid,root)
                self.assertEqual([x[0] for x in messages], ['1','4','5'])
                self.assertIn('未同步的非文本附件: input_image', messages[-1][3])
                self.assertNotIn('secret-not-to-copy', messages[-1][3])
                with self.assertRaises(ValueError): history.messages(str(root),str(uuid.uuid4()),root)
                with self.assertRaises(RuntimeError): history.messages(str(root/'other'),tid,root)

    def test_user_owned_chat_moved_to_project_uses_latest_turn_cwd(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            base = Path(tmp).resolve(); old = base/'old'; project = base/'project'
            old.mkdir(); project.mkdir()
            tid = str(uuid.uuid4())
            path = base / ('rollout-2026-01-01T00-00-00-' + tid + '.jsonl')
            def line(typ, payload): return json.dumps({'type':typ,'timestamp':'2026','payload':payload})+'\n'
            content = line('session_meta', {'id':tid,'cwd':str(old),'thread_source':'agent_created_thread'})
            content += line('turn_context', {'cwd':str(old)})
            content += line('response_item', {'type':'message','id':'1','role':'user','content':[{'type':'input_text','text':'hello'}]})
            content += line('turn_context', {'cwd':str(project)})
            path.write_text(content,encoding='utf-8')
            with patch.dict(os.environ, {'CODEX_THREAD_ID':tid}):
                self.assertEqual(history.messages(str(project),tid,base)[0][0], '1')
                with self.assertRaises(RuntimeError): history.messages(str(old),tid,base)
                path.write_text(content.replace('agent_created_thread','subagent'),encoding='utf-8')
                with self.assertRaises(RuntimeError): history.messages(str(project),tid,base)


if __name__ == '__main__': unittest.main()
