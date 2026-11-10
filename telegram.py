"""Optional Telegram approval bridge. Environment credentials; one poller only."""
import argparse, contextlib, json, os, re, time, urllib.request
from pathlib import Path
from crm import CRM, ROOT

def api(method, payload):
    token=os.environ.get('TELEGRAM_BOT_TOKEN','')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token): raise ValueError('Set TELEGRAM_BOT_TOKEN in the local environment')
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/'+method,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=35) as r: result=json.load(r)
        if not result.get('ok'):raise ValueError()
        return result['result']
    except Exception:raise RuntimeError('Telegram request failed. Check token, network, webhook and competing listeners; secret values are hidden.') from None

def paired():
    chat=os.environ.get('TELEGRAM_CHAT_ID',''); user=os.environ.get('TELEGRAM_USER_ID','')
    if not chat.isdigit() or chat!=user:raise ValueError('Set matching positive TELEGRAM_CHAT_ID and TELEGRAM_USER_ID for your private bot chat')
    return chat,user

@contextlib.contextmanager
def singleton():
    path=ROOT/'data'/'telegram.lock';path.parent.mkdir(exist_ok=True)
    handle=open(path,'a+')
    try:
        if os.name=='nt':
            import msvcrt
            handle.seek(0);handle.write('0');handle.flush();handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close();raise RuntimeError('Another Telegram listener is active. Stop it before restarting.') from None
    try:yield
    finally:handle.close()

def init(crm):
    crm.db.executescript('CREATE TABLE IF NOT EXISTS telegram_previews(chat TEXT, message INTEGER, draft TEXT, version INTEGER, hash TEXT, PRIMARY KEY(chat,message)); CREATE TABLE IF NOT EXISTS telegram_cursor(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER); INSERT OR IGNORE INTO telegram_cursor VALUES(1,0);')
    crm.db.commit()

def notify(crm, identity):
    chat,_=paired(); d=next(x for x in crm.snapshot()['drafts'] if x['id']==identity)
    text=f"To: {d['recipient']}\nSubject: {d['subject']}\nDraft {d['id']} · v{d['version']}\n\n{d['body']}\n\nAttachments: "+(', '.join(Path(a['path']).name+' ['+a['sha256'][:12]+']' for a in d['attachments']) or 'none')
    # Chunk without dropping any copy; the final message binds the complete draft.
    for i in range(0,len(text),3500):api('sendMessage',{'chat_id':chat,'text':text[i:i+3500]})
    result=api('sendMessage',{'chat_id':chat,'text':f"Decision for {d['recipient']} · {d['subject']}\nDraft {d['id']} · version {d['version']}\nApproval covers the complete text and attachment hashes above.", 'reply_markup':{'inline_keyboard':[[{'text':'Approve this version','callback_data':f"a|{d['id']}|{d['version']}|{d['hash'][:12]}"},{'text':'Reject','callback_data':f"r|{d['id']}|{d['version']}|{d['hash'][:12]}"}]]}})
    with crm.db:crm.db.execute('INSERT OR REPLACE INTO telegram_previews VALUES(?,?,?,?,?)',(chat,result['message_id'],d['id'],d['version'],d['hash']))
    return {'notified':True}

def process(crm, update):
    chat,user=paired(); cb=update.get('callback_query'); m=(cb or {}).get('message',{}) if cb else update.get('message',{}); sender=(cb or m).get('from',{}).get('id')
    if str(m.get('chat',{}).get('id'))!=chat or str(sender)!=user or m.get('forward_origin') or m.get('forward_from'):return 'Ignored unauthorized update'
    target=m.get('message_id') if cb else m.get('reply_to_message',{}).get('message_id')
    row=crm.db.execute('SELECT draft,version,hash FROM telegram_previews WHERE chat=? AND message=?',(chat,target)).fetchone()
    if not row:return 'Reply to the specific decision message or use its buttons; nothing approved.'
    mid,version,full_hash=row
    decision=m.get('text','').strip().lower().rstrip('.!')
    if cb:
        expected=[f'a|{mid}|{version}|{full_hash[:12]}',f'r|{mid}|{version}|{full_hash[:12]}']
        if cb.get('data') not in expected:return 'Stale or invalid decision'
        decision='approve' if cb['data']==expected[0] else 'reject'
    if decision not in ['approve','approved','reject','rejected']:return 'Use Approve or Reject on the specific draft. Ask your agent to revise rejected copy.'
    action='approve' if decision.startswith('approv') else 'reject'
    try:
        d=crm.run(action,{'id':mid,'version':version,'hash':full_hash})
        result=f"{action.title()} recorded: {d['recipient']} · {d['subject']} · v{version}. " + ('The connected agent can send after fresh mailbox checks. This listener does not send email.' if action=='approve' else 'No email will be sent. Ask the agent for a revision.')
    except (ValueError,StopIteration):result='Stale decision: review the newest draft. Nothing approved.'
    if cb:api('answerCallbackQuery',{'callback_query_id':cb['id'],'text':result[:180]})
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['doctor','notify','watch']);p.add_argument('id',nargs='?');a=p.parse_args()
    crm=CRM();init(crm)
    if a.command=='doctor':
        me=api('getMe',{}); info=api('getWebhookInfo',{}); paired()
        if info.get('url'):raise ValueError('A webhook is active. Choose webhook OR polling; remove it only after confirming ownership of the other integration.')
        print(json.dumps({'bot':me['username'],'private_pair_configured':True,'webhook_clear':True,'next':'Send /start to your bot and test a fictional draft approval/rejection before enabling any connector sends.'}));return
    if a.command=='notify':print(json.dumps(notify(crm,a.id)));return
    with singleton():
        print('Telegram listener active; keep this process running. No email is sent by this process.',flush=True)
        while True:
            try:
                offset=crm.db.execute('SELECT value FROM telegram_cursor WHERE id=1').fetchone()[0]
                for update in api('getUpdates',{'offset':offset,'timeout':20,'allowed_updates':['message','callback_query']}):
                    result=process(crm,update)
                    if result!='Ignored unauthorized update':api('sendMessage',{'chat_id':paired()[0],'text':result})
                    with crm.db:crm.db.execute('UPDATE telegram_cursor SET value=? WHERE id=1',(update['update_id']+1,))
            except Exception:print('Listener retrying after a connection or validation error. Credentials hidden.',flush=True);time.sleep(10)

if __name__=='__main__':main()
