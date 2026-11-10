"""Private, local CRM and exact-version outbox. Python 3.10+, standard library."""
import argparse, contextlib, hashlib, hmac, io, json, os, re, secrets, sqlite3, time, uuid, zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
MODE = json.loads((ROOT / 'product.json').read_text())['mode']
DEFAULT_STAGES = ['Research', 'Qualified', 'Contacted', 'Conversation', 'Interview', 'Offer', 'Closed'] if MODE == 'jobs' else ['Research', 'Qualified', 'Contacted', 'Discovery', 'Proposal', 'Negotiation', 'Won', 'Lost']

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def clean_email(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', value):
        raise ValueError('Enter one valid email address')
    return value

class CRM:
    def __init__(self, path=None):
        os.umask(0o077)
        path = Path(path or os.environ.get('CRM_DATABASE', ROOT / 'data' / 'crm.sqlite3'))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20)
        os.chmod(path, 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO state VALUES (1, ?)', (json.dumps({'settings': {}, 'contacts': [], 'drafts': [], 'applications': [], 'events': [], 'updates': [], 'mailbox': {}}),))
        self.db.commit()

    def snapshot(self):
        return json.loads(self.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])

    def run(self, action, p=None):
        p = p or {}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            s = self.snapshot()
            result = self.change(s, action, p)
            s['events'].append({'time': time.time(), 'action': action, 'id': p.get('id', ''), 'result_id': result.get('id', '') if isinstance(result, dict) else ''})
            self.db.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(s),))
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def change(self, s, action, p):
        now = time.time()
        def find(kind, identity):
            return next(x for x in s[kind] if x['id'] == identity)
        def contact(identity):
            return find('contacts', identity)
        def envelope(d):
            return {k: d[k] for k in ['contact_id', 'recipient', 'subject', 'body', 'attachments', 'kind', 'parent_id']}
        if action == 'setup':
            settings = dict(p)
            for field in ['locations', 'fields', 'sender', 'timezone', 'provider']:
                if not settings.get(field): raise ValueError('Missing setup field: ' + field)
            settings['sender'] = clean_email(settings['sender'])
            if settings['provider'] not in ['Apollo', 'Lusha']: raise ValueError('Choose Apollo or Lusha')
            stages = settings.get('stages') or DEFAULT_STAGES
            if not isinstance(stages, list) or len(stages) < 2 or len(stages) != len(set(stages)) or any(not isinstance(x,str) or not x.strip() for x in stages): raise ValueError('Use at least two distinct stages')
            if any(c['stage'] not in stages for c in s['contacts']): raise ValueError('Move existing contacts before removing their stage')
            settings['stages'] = stages
            settings['followup_days'] = int(settings.get('followup_days', 3))
            if settings['followup_days'] not in [2, 3]: raise ValueError('Follow-up interval must be 2 or 3 days')
            settings['max_followups'] = min(4, max(0, int(settings.get('max_followups', 2))))
            if MODE == 'bdm' and any(not settings.get(k) for k in ['product', 'problem', 'stakeholders']): raise ValueError('Describe the product, customer problem and stakeholders')
            # Editing setup is not implicit consent to submit applications.
            settings['application_consent'] = s['settings'].get('application_consent', None)
            settings['cvs'] = s['settings'].get('cvs', [])
            s['settings'] = settings
            return {'saved': True}
        if action == 'consent':
            if MODE != 'jobs': raise ValueError('Application consent belongs to the job agent')
            s['settings']['application_consent'] = {'active': p.get('active') is True, 'at': now, 'scope': {k: s['settings'].get(k) for k in ['locations', 'fields']}}
            return s['settings']['application_consent']
        if action == 'cv':
            path = Path(p['path']).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() != '.pdf': raise ValueError('Choose an existing PDF CV')
            cv = {'id': uuid.uuid4().hex, 'label': p['label'], 'fields': p['fields'], 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            s['settings'].setdefault('cvs', []).append(cv)
            return cv
        if action == 'contact':
            if not s['settings']: raise ValueError('Complete setup first')
            company = p['company'].strip().lower()
            if not company or not p.get('name') or not p.get('role'): raise ValueError('Company, contact name and role required')
            mail = clean_email(p['email'])
            if any(c['email'] == mail for c in s['contacts']): raise ValueError('Contact already exists')
            c = {k: str(p.get(k, '')) for k in ['name', 'role', 'source', 'observation', 'problem', 'idea', 'metric', 'location']}
            c.update(id=uuid.uuid4().hex, company=company, email=mail, verified=p.get('verified') is True,
                     stage=s['settings']['stages'][0], state='active', defer_until=0, created=now)
            s['contacts'].append(c)
            return c
        if action == 'stage':
            if p['stage'] not in s['settings']['stages']: raise ValueError('Unknown stage')
            c = contact(p['id']); c['stage'] = p['stage']; return c
        if action == 'mailbox':
            if p.get('complete') is not True or not p.get('evidence'): raise ValueError('Complete Inbox AND Sent review and provide evidence')
            s['mailbox'][p['company'].strip().lower()] = {'at': now, 'evidence': p['evidence']}
            return {'recorded': True}
        if action == 'inbound':
            c = contact(p['id'])
            kind = p['kind']
            if kind not in ['human_reply', 'optout', 'bounce', 'rejected', 'ooo', 'automatic']: raise ValueError('Unknown inbound type')
            if not p.get('provider_id'): raise ValueError('Inbound provider ID required')
            if kind == 'automatic': return {'logged': True}
            targets = [x for x in s['contacts'] if x['company'] == c['company']] if kind in ['human_reply', 'optout', 'rejected'] else [c]
            for x in targets:
                x['state'] = kind
                if kind == 'ooo': x['defer_until'] = float(p['defer_until'])
            return {'paused_contacts': len(targets)}
        if action == 'draft':
            c = contact(p['contact_id'])
            if not c['verified'] or not c['source']: raise ValueError('Verified work email and current role evidence required')
            kind = p.get('kind', 'initial')
            if kind not in ['initial', 'followup', 'reply']: raise ValueError('Unknown draft type')
            if not p.get('subject', '').strip() or not p.get('body', '').strip() or '\n' in p['subject'] or '\r' in p['subject']: raise ValueError('Valid subject and body required')
            attachments = []
            for a in p.get('attachments', []):
                path = Path(a).expanduser().resolve()
                if not path.is_file(): raise ValueError('Attachment does not exist')
                attachments.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            d = {'id': uuid.uuid4().hex, 'contact_id': c['id'], 'recipient': c['email'], 'subject': p['subject'], 'body': p['body'], 'kind': kind,
                 'attachments': attachments, 'parent_id': p.get('parent_id', ''), 'version': 1, 'state': 'awaiting_approval', 'approved_hash': None, 'created': now}
            d['hash'] = digest(envelope(d))
            if any(x['hash'] == d['hash'] for x in s['drafts']): raise ValueError('Identical draft already exists')
            s['drafts'].append(d); return d
        if action in ['approve', 'reject', 'revise', 'claim', 'result']:
            d = find('drafts', p['id']); c = contact(d['contact_id'])
            if action in ['approve', 'reject']:
                if p.get('hash') != d['hash'] or p.get('version') != d['version']: raise ValueError('Stale approval: use current version and full hash')
                if d['state'] not in ['awaiting_approval', 'ready', 'rejected']: raise ValueError('Draft is no longer editable')
                d['state'] = 'ready' if action == 'approve' else 'rejected'; d['approved_hash'] = d['hash'] if action == 'approve' else None
            elif action == 'revise':
                if d['state'] not in ['awaiting_approval', 'ready', 'rejected']: raise ValueError('Cannot revise a dispatched draft')
                if not p.get('subject') or not p.get('body') or '\n' in p['subject'] or '\r' in p['subject']: raise ValueError('Valid subject and body required')
                d.update(subject=p['subject'], body=p['body'], version=d['version']+1, state='awaiting_approval', approved_hash=None)
                d['hash'] = digest(envelope(d))
            elif action == 'claim':
                if not s['settings'].get('sender'): raise ValueError('Complete setup first')
                if d['state'] != 'ready' or d['approved_hash'] != d['hash']: raise ValueError('Exact approval required for every email')
                peers = [x for x in s['contacts'] if x['company'] == c['company']]
                if any(x['state'] in ['optout', 'rejected'] for x in peers) or c['state'] == 'bounce': raise ValueError('Contact or company is suppressed')
                if any(x['state'] == 'human_reply' for x in peers) and d['kind'] != 'reply': raise ValueError('Human reply pauses company outreach')
                if c['defer_until'] > now: raise ValueError('Contact is out of office')
                review = s['mailbox'].get(c['company'], {})
                if now - review.get('at', 0) > 900: raise ValueError('Fresh completed Inbox/Sent review required within 15 minutes')
                peer_ids = {x['id'] for x in peers}
                history = [x for x in s['drafts'] if x['contact_id'] in peer_ids and x['id'] != d['id']]
                if any(x['state'] in ['sending', 'unknown'] for x in history): raise ValueError('Uncertain or pending company send must be reconciled')
                sent = [x for x in history if x['state'] == 'sent']
                if d['kind'] == 'initial' and sent:
                    if any(x['contact_id'] == c['id'] for x in sent): raise ValueError('Initial email already sent to this contact')
                    last = max(sent, key=lambda x: x['sent_at'])
                    if now - last['sent_at'] < s['settings']['followup_days']*86400: raise ValueError('Company cooldown not reached')
                    prior = contact(last['contact_id'])
                    sequence = [x for x in sent if x['contact_id'] == prior['id']]
                    if prior['state'] != 'bounce' and sum(x['kind'] == 'followup' for x in sequence) < s['settings']['max_followups']: raise ValueError('Finish the current stakeholder follow-up sequence first')
                    if not p.get('switch_reason'): raise ValueError('Explicit stakeholder switch rationale required')
                    for peer in peers:
                        if peer['id'] != c['id'] and peer['state'] == 'active': peer['state'] = 'switched'
                if c['state'] == 'switched': raise ValueError('Another stakeholder now owns this company sequence')
                if d['kind'] == 'followup':
                    own = [x for x in sent if x['contact_id'] == c['id']]
                    if not own: raise ValueError('Follow-up requires a confirmed initial send')
                    last = max(own, key=lambda x: x['sent_at'])
                    if now-last['sent_at'] < s['settings']['followup_days']*86400: raise ValueError('Follow-up is not due')
                    if sum(x['kind']=='followup' for x in own) >= s['settings']['max_followups']: raise ValueError('Sequence limit reached')
                    if d['parent_id'] != last['provider_id']: raise ValueError('Follow-up must reference the latest provider message')
                if d['kind'] == 'reply' and not d['parent_id']: raise ValueError('Actual incoming message ID required for replies')
                for a in d['attachments']:
                    if hashlib.sha256(Path(a['path']).read_bytes()).hexdigest() != a['sha256']: raise ValueError('Attachment changed; create a new draft and approval')
                d.update(state='sending', claim_token=secrets.token_hex(24), claimed_at=now)
            elif action == 'result':
                if not hmac.compare_digest(str(p.get('claim_token', '')), str(d.get('claim_token', '-'))): raise ValueError('Invalid claim token')
                if d['state'] != 'sending': raise ValueError('Already finalized; do not retry')
                if p['outcome'] not in ['sent', 'unknown']: raise ValueError('Use sent or unknown')
                if p['outcome'] == 'sent' and not p.get('provider_id'): raise ValueError('Provider confirmation required')
                d.update(state=p['outcome'], provider_id=p.get('provider_id', ''), sent_at=now)
            return d
        if action == 'application_claim':
            consent = s['settings'].get('application_consent') or {}
            scope = {k: s['settings'].get(k) for k in ['locations', 'fields']}
            if not consent.get('active') or consent['scope'] != scope: raise ValueError('Application consent missing, revoked or outside updated scope')
            if p['location'] not in scope['locations'] or p['field'] not in scope['fields']: raise ValueError('Application outside consent scope')
            if urlparse(p['url']).scheme != 'https': raise ValueError('Use a verified HTTPS career page')
            cv = next(x for x in s['settings']['cvs'] if x['id']==p['cv_id'])
            if p['field'] not in cv['fields']: raise ValueError('CV does not match this field')
            if hashlib.sha256(Path(cv['path']).read_bytes()).hexdigest() != cv['sha256']: raise ValueError('CV changed; register it again')
            key = digest([p['company'].strip().lower(), p['vacancy_id'].strip().lower()])
            if any(x['key']==key for x in s['applications']): raise ValueError('Application already reserved, submitted or uncertain')
            a = {k: p[k] for k in ['company','vacancy_id','location','field','url','cv_id']}
            a.update(id=uuid.uuid4().hex, key=key, state='submitting', created=now)
            s['applications'].append(a); return a
        if action == 'application_result':
            a = find('applications', p['id'])
            if a['state'] != 'submitting': raise ValueError('Application already finalized')
            if p['state'] not in ['submitted', 'unknown']: raise ValueError('Use submitted or unknown')
            if p['state']=='submitted' and not p.get('evidence'): raise ValueError('Confirmation evidence required')
            a.update(state=p['state'], evidence=p.get('evidence','')); return a
        if action == 'recover':
            for d in s['drafts']:
                if d['state']=='sending': d['state']='unknown'
            for a in s['applications']:
                if a['state']=='submitting': a['state']='unknown'
            return {'recovered': True, 'note': 'Uncertain operations remain blocked; reconcile provider history manually.'}
        raise ValueError('Unknown action')

    def due(self):
        s=self.snapshot(); now=time.time(); out=[]
        for c in s['contacts']:
            if c['state'] not in ['active','ooo'] or c['defer_until'] > now: continue
            peers=[x['id'] for x in s['contacts'] if x['company']==c['company']]
            if any(x['state'] in ['human_reply','optout','rejected'] for x in s['contacts'] if x['id'] in peers): continue
            if any(d['state'] in ['sending','unknown','awaiting_approval','ready','rejected'] for d in s['drafts'] if d['contact_id'] in peers): continue
            own=[d for d in s['drafts'] if d['contact_id']==c['id'] and d['state']=='sent']
            if not own: continue
            last=max(own,key=lambda d:d['sent_at']); when=last['sent_at']+s['settings']['followup_days']*86400
            if when<=now: out.append({'contact_id':c['id'],'company':c['company'],'due_at':when,'parent_id':last['provider_id'], 'action':'research_alternate_stakeholder' if sum(d['kind']=='followup' for d in own)>=s['settings']['max_followups'] else 'draft_followup'})
        return out

def workbook(s):
    """Portable Excel export: all untrusted values are literal inline strings."""
    sheets=[('Contacts', ['company','name','role','email','stage','state','location','source'], s['contacts']),
            ('Outbox',['id','recipient','subject','kind','version','state','provider_id'],s['drafts']),
            ('Applications',['company','vacancy_id','location','field','state','url'],s['applications'])]
    b=io.BytesIO()
    def col(n):
        v=''
        while n: n,r=divmod(n-1,26);v=chr(65+r)+v
        return v
    def xml_text(v): return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(v)))
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,4))+'</Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,4))+'</Relationships>')
        for i,(name,fields,records) in enumerate(sheets,1):
            rows=[fields]+[[r.get(k,'') for k in fields] for r in records]
            content=''.join('<row r="%d">'%n+''.join(f'<c r="{col(j)}{n}" t="inlineStr"><is><t xml:space="preserve">{xml_text(v)}</t></is></c>' for j,v in enumerate(row,1))+'</row>' for n,row in enumerate(rows,1))
            z.writestr(f'xl/worksheets/sheet{i}.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols>'+f'<col min="1" max="{len(fields)}" width="26" customWidth="1"/>'+'</cols><sheetData>'+content+f'</sheetData><autoFilter ref="A1:{col(len(fields))}{len(rows)}"/></worksheet>')
    return b.getvalue()

def serve(port):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,code,body,kind='application/json'):
            self.send_response(code); self.send_header('Content-Type',kind); self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff'); self.end_headers();self.wfile.write(body)
        def authorized(self): return hmac.compare_digest(self.headers.get('X-CRM-Token',''),token)
        def do_GET(self):
            if self.path=='/':
                return self.reply(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
            if not self.authorized(): return self.reply(401,b'{}')
            crm=CRM()
            try:
                if self.path=='/api/state':
                    s=crm.snapshot()
                    for d in s['drafts']: d.pop('claim_token',None)
                    return self.reply(200,json.dumps({'mode':MODE,'state':s,'due':crm.due()}).encode())
                if self.path=='/api/export': return self.reply(200,workbook(crm.snapshot()),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                return self.reply(404,b'{}')
            finally: crm.db.close()
        def do_POST(self):
            if not self.authorized():return self.reply(401,b'{}')
            allowed={'setup','consent','contact','stage','draft','approve','reject','revise'}
            action=self.path.removeprefix('/api/')
            if action not in allowed:return self.reply(403,b'{}')
            crm=CRM()
            try:
                size=int(self.headers.get('Content-Length','0'))
                if size>100000:raise ValueError('Request too large')
                result=crm.run(action,json.loads(self.rfile.read(size)))
                self.reply(200,json.dumps(result).encode())
            except (ValueError,KeyError,StopIteration,TypeError) as e:self.reply(400,json.dumps({'error':str(e) or 'Record not found'}).encode())
            finally:crm.db.close()
    print(f'Open http://127.0.0.1:{port}/#{token}',flush=True)
    HTTPServer(('127.0.0.1',port),Handler).serve_forever()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['serve','state','due','action','export']);parser.add_argument('action',nargs='?');parser.add_argument('--input');parser.add_argument('--output',default='data/crm.xlsx');parser.add_argument('--port',type=int,default=8765)
    a=parser.parse_args()
    if a.command=='serve':return serve(a.port)
    crm=CRM()
    if a.command=='export':
        path=Path(a.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(workbook(crm.snapshot()));print('Export saved');return
    result=crm.snapshot() if a.command=='state' else crm.due() if a.command=='due' else crm.run(a.action,json.loads(Path(a.input).read_text()) if a.input else json.load(__import__('sys').stdin))
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
