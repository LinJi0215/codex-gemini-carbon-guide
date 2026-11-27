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
            path.write_text(text, encoding='utf-8')
            with patch.dict(os.environ, {'CODEX_THREAD_ID':tid}):
                messages = history.messages(str(root),tid,root)
                self.assertEqual([x[0] for x in messages], ['1','4'])
                with self.assertRaises(ValueError): history.messages(str(root),str(uuid.uuid4()),root)
                with self.assertRaises(RuntimeError): history.messages(str(root/'other'),tid,root)


if __name__ == '__main__': unittest.main()
