import io, json, tempfile, time, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from crm import CRM, MODE, workbook
import telegram

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.crm=CRM(Path(self.tmp.name)/'db.sqlite3');self.addCleanup(self.crm.db.close)
        self.crm.run('setup',{'locations':['London'],'fields':['Marketing'],'sender':'owner@example.com','timezone':'UTC','provider':'Apollo','stages':['Research','Contacted','Won'],'product':'Research service','problem':'Manual reporting','stakeholders':'Operations directors','followup_days':2,'max_followups':1})
        self.contact=self.crm.run('contact',{'company':'Example Company','name':'Taylor Example','role':'Director','email':'taylor@example.com','source':'https://example.com/team','verified':True})
    def draft(self,kind='initial',parent='',contact=None):
        return self.crm.run('draft',{'contact_id':(contact or self.contact)['id'],'subject':'A practical idea','body':'Could a small reporting pilot help?','kind':kind,'parent_id':parent})
    def approve(self,d):return self.crm.run('approve',{'id':d['id'],'version':d['version'],'hash':d['hash']})
    def review(self):self.crm.run('mailbox',{'company':'Example Company','complete':True,'evidence':'Fixture: both Inbox and Sent fully checked'})
    def send(self,d):
        self.approve(d);self.review();claimed=self.crm.run('claim',{'id':d['id']})
        return self.crm.run('result',{'id':d['id'],'claim_token':claimed['claim_token'],'outcome':'sent','provider_id':'provider-'+d['id']})
    def test_stage_removal_cannot_hide_contacts(self):
        settings=self.crm.snapshot()['settings'];settings['stages']=['Contacted','Won']
        with self.assertRaisesRegex(ValueError,'Move existing'):self.crm.run('setup',settings)
    def test_stakeholder_switch_waits_for_sequence(self):
        self.send(self.draft())
        other=self.crm.run('contact',{'company':'Example Company','name':'Jordan Example','role':'Manager','email':'jordan@example.com','source':'https://example.com/team','verified':True})
        d=self.draft(contact=other);self.approve(d)
        with patch('crm.time.time',return_value=time.time()+3*86400):
            self.review()
            with self.assertRaisesRegex(ValueError,'Finish the current'):self.crm.run('claim',{'id':d['id'],'switch_reason':'Another relevant role'})
    def test_initial_requires_approval(self):
        d=self.draft();self.review()
        with self.assertRaisesRegex(ValueError,'approval'):self.crm.run('claim',{'id':d['id']})
    def test_revision_invalidates_approval_and_stale_buttons(self):
        d=self.draft();self.approve(d)
        self.crm.run('revise',{'id':d['id'],'subject':'Changed','body':'Different exact copy'})
        with self.assertRaisesRegex(ValueError,'Stale'):self.approve(d)
        self.review()
        with self.assertRaises(ValueError):self.crm.run('claim',{'id':d['id']})
    def test_fresh_complete_mailbox_required(self):
        d=self.draft();self.approve(d)
        with self.assertRaisesRegex(ValueError,'Inbox/Sent'):self.crm.run('claim',{'id':d['id']})
        with self.assertRaises(ValueError):self.crm.run('mailbox',{'company':'Example Company','complete':False,'evidence':'Partial'})
        self.review()
        with patch('crm.time.time',return_value=time.time()+901):
            with self.assertRaises(ValueError):self.crm.run('claim',{'id':d['id']})
    def test_uncertain_send_never_retried(self):
        d=self.draft();self.approve(d);self.review();c=self.crm.run('claim',{'id':d['id']})
        self.crm.run('result',{'id':d['id'],'claim_token':c['claim_token'],'outcome':'unknown'})
        with self.assertRaises(ValueError):self.crm.run('claim',{'id':d['id']})
        self.assertEqual(self.crm.due(),[])
    def test_parallel_connection_cannot_claim_twice(self):
        d=self.draft();self.approve(d);self.review();self.crm.run('claim',{'id':d['id']})
        other=CRM(Path(self.tmp.name)/'db.sqlite3')
        try:
            with self.assertRaises(ValueError):other.run('claim',{'id':d['id']})
        finally:other.db.close()
    def test_followup_due_from_actual_send_and_parent_checked(self):
        sent=self.send(self.draft())
        f=self.draft('followup','wrong-parent');self.approve(f);self.review()
        with self.assertRaises(ValueError):self.crm.run('claim',{'id':f['id']})
        with patch('crm.time.time',return_value=time.time()+3*86400):
            self.review()
            with self.assertRaisesRegex(ValueError,'latest provider'):self.crm.run('claim',{'id':f['id']})
    def test_company_human_reply_stops_all_sequences(self):
        self.send(self.draft());self.crm.run('inbound',{'id':self.contact['id'],'kind':'human_reply','provider_id':'reply-1'})
        with patch('crm.time.time',return_value=time.time()+3*86400):self.assertEqual(self.crm.due(),[])
    def test_optout_even_blocks_approved_reply(self):
        self.crm.run('inbound',{'id':self.contact['id'],'kind':'optout','provider_id':'optout-1'})
        d=self.draft('reply','optout-1');self.approve(d);self.review()
        with self.assertRaisesRegex(ValueError,'suppressed'):self.crm.run('claim',{'id':d['id']})
    def test_ooo_delays(self):
        d=self.draft();self.approve(d);self.review();self.crm.run('inbound',{'id':self.contact['id'],'kind':'ooo','provider_id':'ooo1','defer_until':time.time()+86400})
        with self.assertRaisesRegex(ValueError,'office'):self.crm.run('claim',{'id':d['id']})
    def test_attachment_mutation_blocks_send(self):
        p=Path(self.tmp.name)/'cv.pdf';p.write_bytes(b'%PDF-fixture')
        d=self.crm.run('draft',{'contact_id':self.contact['id'],'subject':'CV','body':'Attached','attachments':[str(p)]});self.approve(d);self.review();p.write_bytes(b'%PDF-changed')
        with self.assertRaisesRegex(ValueError,'Attachment changed'):self.crm.run('claim',{'id':d['id']})
    def test_provider_id_and_claim_token_required(self):
        d=self.draft();self.approve(d);self.review();c=self.crm.run('claim',{'id':d['id']})
        with self.assertRaises(ValueError):self.crm.run('result',{'id':d['id'],'claim_token':'wrong','outcome':'sent','provider_id':'1'})
        with self.assertRaises(ValueError):self.crm.run('result',{'id':d['id'],'claim_token':c['claim_token'],'outcome':'sent'})
    def test_recovery_holds_interrupted_send(self):
        d=self.draft();self.approve(d);self.review();self.crm.run('claim',{'id':d['id']});self.crm.run('recover')
        self.assertEqual(self.crm.snapshot()['drafts'][0]['state'],'unknown')
    def test_export_is_actual_xlsx_and_formula_safe(self):
        s=self.crm.snapshot();s['contacts'][0]['name']='=HYPERLINK("bad")'
        with zipfile.ZipFile(io.BytesIO(workbook(s))) as z:
            xml=z.read('xl/worksheets/sheet1.xml').decode();self.assertIn('t="inlineStr"',xml);self.assertNotIn('<f>',xml);self.assertIn('autoFilter',xml)
            import xml.etree.ElementTree as ET
            for n in z.namelist():ET.fromstring(z.read(n))
    def test_telegram_wrong_user_and_ambiguous_approval(self):
        telegram.init(self.crm)
        with patch.dict('os.environ',{'TELEGRAM_CHAT_ID':'123','TELEGRAM_USER_ID':'123'}):
            result=telegram.process(self.crm,{'message':{'chat':{'id':123},'from':{'id':999},'text':'approve'}});self.assertIn('unauthorized',result)
            result=telegram.process(self.crm,{'message':{'chat':{'id':123},'from':{'id':123},'text':'approve'}});self.assertIn('specific',result)
    def test_telegram_exact_preview_approval(self):
        d=self.draft();telegram.init(self.crm)
        with self.crm.db:self.crm.db.execute('INSERT INTO telegram_previews VALUES(?,?,?,?,?)',('123',10,d['id'],d['version'],d['hash']))
        with patch.dict('os.environ',{'TELEGRAM_CHAT_ID':'123','TELEGRAM_USER_ID':'123'}):
            r=telegram.process(self.crm,{'message':{'chat':{'id':123},'from':{'id':123},'text':'approve','reply_to_message':{'message_id':10}}})
        self.assertIn('Approve recorded',r);self.assertEqual(self.crm.snapshot()['drafts'][0]['state'],'ready')
    @unittest.skipUnless(MODE=='jobs','Job-only feature')
    def test_consent_revocation_scope_cv_and_dedup(self):
        p=Path(self.tmp.name)/'cv.pdf';p.write_bytes(b'%PDF-fixture')
        cv=self.crm.run('cv',{'path':str(p),'label':'Marketing CV','fields':['Marketing']})
        payload={'company':'Example','vacancy_id':'123','url':'https://example.com/jobs/123','field':'Marketing','location':'London','cv_id':cv['id']}
        with self.assertRaises(ValueError):self.crm.run('application_claim',payload)
        self.crm.run('consent',{'active':True});self.crm.run('application_claim',payload)
        with self.assertRaises(ValueError):self.crm.run('application_claim',payload)
        self.crm.run('consent',{'active':False});payload['vacancy_id']='456'
        with self.assertRaises(ValueError):self.crm.run('application_claim',payload)

if __name__=='__main__':unittest.main()
