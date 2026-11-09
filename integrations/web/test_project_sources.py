import json
import base64
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from broker import Broker
from test_broker import Provider
from project_sources import project_url

class Cloud(Provider):
    def api(self,method,path,body=None):
        if path=='contexts':return {'id':'00000000-0000-4000-8000-000000000002'}
        if path.endswith('/debug'):return {'debuggerFullscreenUrl':'https://browserbase.com/private-fixture'}
        raise AssertionError('Unexpected API operation')
    def create(self,rid,practice=False,timeout_seconds=2700,context_id=None):
        self.context=context_id
        return super().create(rid,practice,timeout_seconds)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.p=Cloud();self.b=Broker(Path(self.tmp.name)/'browser.db',self.p)
        self.url_patch=patch('broker.url',side_effect=lambda v:(v,'https://chatgpt.com'));self.url_patch.start();self.addCleanup(self.url_patch.stop)
        self.b.url_validator=lambda v:(v,'https://chatgpt.com')
    def test_only_native_owner_can_connect_and_confirm_and_no_context_id_leaks(self):
        for action in ['project_connect','project_ready']:
            with self.assertRaises(ValueError):self.b.call(action,{'service':'chatgpt'})
        connected=self.b.call('project_connect',{'service':'chatgpt'},True)
        self.assertIn('live_view_url',connected);self.assertNotIn(self.p.context,json.dumps(connected))
        self.assertFalse(connected['file_access_verified'])
        with self.assertRaisesRegex(ValueError,'sign-in is pending'):self.b.call('snapshot',{'id':connected['session_id']})
        with self.assertRaises(ValueError):self.b.call('project_read',{'service':'chatgpt'})
    def test_connected_project_browser_cannot_prepare_tasks_or_effects(self):
        r=self.b.call('project_connect',{'service':'chatgpt'},True);sid=r['session_id']
        self.assertTrue(self.b.session(sid)['project_read_only'])
        with self.assertRaisesRegex(ValueError,'read-only'):self.b.call('prepare',{'id':sid,'request_id':'blocked-entry','steps':[{'kind':'fill','ref':'e0','value':'chat text'}]})
        with self.assertRaisesRegex(ValueError,'read-only'):self.b.call('task_prepare',{'session_id':sid})
        self.assertEqual(self.p.acts,0)
    def test_site_paths_and_owner_acknowledgment_do_not_forge_file_access(self):
        self.b.call('project_connect',{'service':'chatgpt'},True)
        r=self.b.call('project_ready',{'service':'chatgpt'},True);self.assertFalse(r['file_access_verified'])
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        r=self.b.call('project_read',{'service':'chatgpt','url':'https://chatgpt.com/g/g-p-abcdef/project'})
        self.assertEqual(r['status'],'page_read');self.assertFalse(r['file_access_verified']);self.assertEqual(self.p.acts,0)
        for url in ['https://chatgpt.com/backend-api/files','https://chatgpt.com/settings','https://other.example/projects','https://chatgpt.com/?token=secret','https://chatgpt.com/c/abcdef']:
            with self.assertRaises(ValueError):project_url('chatgpt',url)
    def test_model_cannot_attach_saved_context_to_a_generic_browser(self):
        with self.assertRaises(ValueError):self.b.call('open',{'request_id':'spoof-context-1','url':'https://chatgpt.com/','_project_service':'chatgpt'})
        self.assertEqual(self.p.created,0)
    def test_file_content_is_required_for_verification_and_cursor_checks(self):
        self.b.call('project_connect',{'service':'chatgpt'},True);self.b.call('project_ready',{'service':'chatgpt'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        target='https://chatgpt.com/g/g-p-fixture/project'
        self.assertEqual(project_url('chatgpt',target+'?tab=sources'),target+'?tab=sources')
        with self.assertRaises(ValueError):project_url('chatgpt',target+'?tab=settings')
        with self.assertRaises(ValueError):self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Fixture.md','offset':10})
        created=self.p.created
        with self.assertRaises(ValueError):self.b.call('project_file',{'service':'chatgpt','file_name':'Fixture.md'})
        self.assertEqual(self.p.created,created)
        self.b.projects.open('chatgpt')
        with patch.object(self.p,'run',return_value={'ok':True,'file':{'name':'Fixture.md','content':'Actual fictional file content.','visible_text_chars':29,'sha256':'a'*64,'project_url':target}}):
            r=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Fixture.md'})
        self.assertEqual(r['status'],'file_read');self.assertTrue(r['file_access_verified']);self.assertEqual(r['last_verified_file']['name'],'Fixture.md')
        with patch.object(self.p,'run',return_value={'ok':False,'kind':'file_changed'}):
            changed=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Fixture.md','offset':10,'expected_sha256':'a'*64})
        self.assertFalse(changed['file_access_verified']);self.assertEqual(changed['status'],'file_changed');self.assertEqual(self.p.acts,0)
    def test_document_sign_in_text_is_not_an_account_login_and_claude_reads_are_scoped(self):
        self.b.call('project_connect',{'service':'claude'},True);self.b.call('project_ready',{'service':'claude'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        self.b.projects.open('claude')
        snap={'url':'https://claude.ai/project/00000000-0000-4000-8000-000000000003','text':'Sign in instructions are source material.','controls':[]}
        with patch.object(self.p,'run',return_value={'ok':True,'snapshot':snap,'files':[{'name':'Sign in instructions.md'}]}) as runner:
            r=self.b.call('project_files',{'service':'claude','project_name':'Project fixture'})
        self.assertEqual(r['status'],'page_read');self.assertFalse(r['file_access_verified']);self.assertEqual(r['files'][0]['name'],'Sign in instructions.md')
        self.assertEqual(runner.call_args.args[1],'claude_sources');self.assertTrue(runner.call_args.kwargs['project_scope'])
        with patch.object(self.p,'run',return_value={'ok':False,'kind':'authentication_required'}):
            r=self.b.call('project_read',{'service':'claude'})
        self.assertEqual(r['status'],'authentication_required');self.assertEqual(self.b.projects.row('claude')['status'],'reauthentication_required')
    def test_project_chats_are_read_only_scoped_and_recorded(self):
        from project_sources import chat_url
        self.b.call('project_connect',{'service':'chatgpt'},True);self.b.call('project_ready',{'service':'chatgpt'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        chat='https://chatgpt.com/g/g-p-fixture/c/6900aaaa-1111-2222-3333-444455556666'
        self.assertEqual(chat_url('chatgpt',chat),chat);self.assertEqual(chat_url('chatgpt','https://chatgpt.com/c/6900aaaa-1111'),'https://chatgpt.com/c/6900aaaa-1111')
        self.assertEqual(chat_url('claude','https://claude.ai/chat/11111111-2222-4333-8444-555555555555'),'https://claude.ai/chat/11111111-2222-4333-8444-555555555555')
        for bad in ['https://chatgpt.com/share/6900aaaa-1111','https://chatgpt.com/backend-api/conversation/6900aaaa-1111',chat+'?model=x',chat+'#x',
                    'https://claude.ai/api/organizations/x/chat_conversations','https://chatgpt.com/settings','http://chatgpt.com/c/6900aaaa-1111','https://claude.ai/chat/11111111-2222-4333-8444-555555555555/share']:
            with self.assertRaises(ValueError):chat_url('chatgpt' if 'chatgpt' in bad else 'claude',bad)
        with self.assertRaises(ValueError):project_url('chatgpt',chat)
        created=self.p.created
        for bad in [{'chat_url':chat,'project_name':'X'},{'chat_title':'Draft'},{'project_name':'X'},{'chat_url':chat,'offset':5}]:
            with self.assertRaises(ValueError):self.b.call('project_chat',{'service':'chatgpt',**bad})
        with self.assertRaises(ValueError):self.b.call('project_chats',{'service':'chatgpt'})
        with self.assertRaises(ValueError):self.b.call('project_files',{'service':'chatgpt','chat_url':chat})
        self.assertEqual(self.p.created,created)
        self.b.projects.open('chatgpt')
        listing={'ok':True,'project_url':'https://chatgpt.com/g/g-p-fixture/project','chats':[{'title':'Draft','url':chat,'preview':'p'}],'complete':True}
        with patch.object(self.p,'run',return_value=listing) as runner:
            r=self.b.call('project_chats',{'service':'chatgpt','project_name':'Legal Matter'})
        self.assertEqual(runner.call_args.args[1],'chatgpt_chats');self.assertEqual(r['status'],'chat_list');self.assertEqual(r['chats'][0]['url'],chat);self.assertFalse(r['file_access_verified'])
        out={'ok':True,'chat':{'title':'Draft','content':'[User]\nHi','visible_text_chars':10,'sha256':'b'*64,'chat_url':chat,'turns':1}}
        with patch.object(self.p,'run',return_value=out) as runner:
            r=self.b.call('project_chat',{'service':'chatgpt','chat_url':chat})
        self.assertEqual(runner.call_args.args[1],'chatgpt_chat');self.assertEqual(runner.call_args.kwargs['read_url'],chat)
        self.assertEqual(r['status'],'chat_read');self.assertTrue(r['file_access_verified']);self.assertEqual(r['last_verified_file']['name'],'chat: Draft')
        with patch.object(self.p,'run',return_value=out) as runner:
            self.b.call('project_chat',{'service':'chatgpt','project_name':'Legal Matter','chat_title':'Draft','max_chars':500})
        self.assertEqual(runner.call_args.kwargs['chat_title'],'Draft');self.assertEqual(self.p.acts,0)
    def test_chat_is_read_once_whole_and_every_page_comes_from_the_same_copy(self):
        # 0.32.2: paging used to re-open and re-render the chat, so pages overlapped or skipped turns ("overlapping portions").
        self.b.call('project_connect',{'service':'chatgpt'},True);self.b.call('project_ready',{'service':'chatgpt'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        self.b.projects.open('chatgpt')
        chat='https://chatgpt.com/c/6900aaaa-1111-2222-3333-444455556666'
        text='Chat: Analyze GME Email\nTurns: 120\n\n'+'\n\n'.join('[%s · 2026-11-0%d 12:00 UTC]\nturn %03d'%('User' if i%2==0 else 'Assistant',1+i%8,i) for i in range(120))
        att=[{'name':'GME Nov 2.eml','mime_type':'message/rfc822','size':5000}]
        out={'ok':True,'chat':{'title':'Analyze GME Email','chat_url':chat,'text':text,'turns':120,'attachments':att,'extraction':'conversation_data','complete':True,'created':'2026-11-01 12:00 UTC','updated':'2026-11-08 12:00 UTC'}}
        with patch.object(self.p,'run',return_value=out) as runner:
            first=self.b.call('project_chat',{'service':'chatgpt','project_name':'Legal Matter','chat_title':'Analyze GME Email','max_chars':1000})
        self.assertEqual(runner.call_count,1);self.assertIs(runner.call_args.kwargs['full'],True);self.assertNotIn('offset',runner.call_args.kwargs)
        c=first['chat'];self.assertEqual(first['status'],'chat_read');self.assertEqual(c['turns'],120);self.assertEqual(c['attachments'],att)
        self.assertTrue(c['complete']);self.assertEqual(c['extraction'],'conversation_data');self.assertEqual(c['served_from'],'read just now')
        pages=[c['content']];sha=c['sha256']
        with patch.object(self.p,'run',side_effect=AssertionError('the browser must not reopen for later pages')):
            while c['next_offset'] is not None:
                r=self.b.call('project_chat',{'service':'chatgpt','project_name':'Legal Matter','chat_title':'Analyze GME Email','offset':c['next_offset'],'expected_sha256':sha,'max_chars':1000})
                c=r['chat'];self.assertEqual(c['sha256'],sha);self.assertEqual(c['served_from'],'copy read at most an hour ago');pages.append(c['content'])
            self.assertEqual(''.join(pages),text)                                   # contiguous, no overlap, nothing missing
            by_link=self.b.call('project_chat',{'service':'chatgpt','chat_url':chat,'max_chars':1000})
        self.assertEqual(by_link['chat']['sha256'],sha)                              # the same copy, asked for by its link
        with patch.object(self.p,'run',return_value=out) as runner:
            self.b.call('project_chat',{'service':'chatgpt','chat_url':chat,'refresh':True})
        self.assertEqual(runner.call_count,1)
        with self.assertRaises(ValueError):self.b.call('project_chat',{'service':'chatgpt','chat_url':chat,'refresh':'yes'})
        with patch.object(self.p,'run',side_effect=AssertionError('no browser for a stale offset')):
            stale=self.b.call('project_chat',{'service':'chatgpt','project_name':'Legal Matter','chat_title':'Another chat','offset':500,'expected_sha256':'c'*64})
        self.assertEqual(stale['status'],'file_changed')
    def test_chat_read_failure_details_reach_the_agent(self):
        self.b.call('project_connect',{'service':'chatgpt'},True);self.b.call('project_ready',{'service':'chatgpt'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        self.b.projects.open('chatgpt')
        fail={'ok':False,'kind':'chat_not_readable','error_stage':'chatgpt_chat_read','dom_probe':{'articles':0}}
        with patch.object(self.p,'run',return_value=fail),patch('sys.stderr') as err:
            r=self.b.call('project_chat',{'service':'chatgpt','chat_url':'https://chatgpt.com/c/6900aaaa-1111'})
        self.assertEqual(r['status'],'chat_not_readable');self.assertEqual(r['dom_probe'],{'articles':0});self.assertFalse(r['file_access_verified'])
        self.assertIn('project_read_failure',''.join(c.args[0] for c in err.write.call_args_list))
    def test_captured_original_is_extracted_and_continued_without_reopening_browser(self):
        from test_extract import make_docx,scanned_pdf,wait,FIXTURES,OCR
        if not (FIXTURES and OCR):self.skipTest('PDF fixture libraries or OCR tools unavailable')
        self.b.call('project_connect',{'service':'chatgpt'},True);self.b.call('project_ready',{'service':'chatgpt'},True)
        with self.b.db() as db:db.execute('UPDATE project_sources SET updated=?',(time.time()-10,))
        self.b.projects.open('chatgpt');target='https://chatgpt.com/g/g-p-fixture/project'
        docx={'ok':True,'original':{'kind':'docx','bytes_b64':base64.b64encode(make_docx(['Dear Counsel,','Revised response.'])).decode(),'size':1},'project_url':target}
        with patch.object(self.p,'run',return_value=docx):
            r=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Reply.docx','max_chars':10})
        self.assertEqual(r['status'],'file_read');self.assertEqual(r['file']['extraction'],'original_docx');self.assertEqual(r['file']['content'],'Dear Couns');self.assertTrue(r['file_access_verified'])
        with patch.object(self.p,'run',side_effect=AssertionError('browser reopened')):
            more=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Reply.docx','offset':10,'expected_sha256':r['file']['sha256']})
        self.assertEqual(more['file']['content'],'el,\nRevised response.')
        scan={'ok':True,'original':{'kind':'pdf','bytes_b64':base64.b64encode(scanned_pdf(['NOTICE OF NONCOMPLIANCE'])).decode(),'size':1},'project_url':target}
        with patch.object(self.p,'run',return_value=scan):
            r=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Scan.pdf'})
        self.assertEqual(r['status'],'ocr_in_progress')
        wait(self.b.projects.extract,json.dumps(['chatgpt',target,'Scan.pdf']))
        with patch.object(self.p,'run',side_effect=AssertionError('browser reopened')):
            r=self.b.call('project_file',{'service':'chatgpt','url':target,'file_name':'Scan.pdf'})
        self.assertEqual(r['file']['extraction'],'ocr');self.assertIn('NONCOMPLIANCE',r['file']['content'].upper());self.assertIn('OCR',r['file']['note'])
