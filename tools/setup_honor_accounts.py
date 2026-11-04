"""One-time local setup; copy only JST encrypted credentials/session, prompt for BN."""
import getpass
import json
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gift_registry.credentials import CredentialStore

def main():
    if ROOT.name!='gifts-ry': raise SystemExit('Only the independent gifts-ry checkout is supported')
    source=ROOT.parent
    path=ROOT/'config/accounts.local.json'
    if path.exists(): raise SystemExit('Account file already exists; use the local account settings instead')
    original=json.loads((source/'config/accounts.local.json').read_bytes())
    if 'jushuitan' not in original['accounts']: raise SystemExit('Source JST account is missing')
    document={'version':1,'accounts':{'jushuitan':original['accounts']['jushuitan']},
              'browser_headless':{'jushuitan':original.get('browser_headless',{}).get('jushuitan',False),'banniu':False}}
    path.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
    session=source/'data/sessions/jushuitan.dpapi'
    if session.exists():
        (ROOT/'data/sessions').mkdir(parents=True,exist_ok=True)
        shutil.copy2(session,ROOT/'data/sessions/jushuitan.dpapi')
    username=input('Honor Banniu username: ').strip()
    password=getpass.getpass('Honor Banniu password (hidden): ')
    try:
        CredentialStore(path).save('banniu',username,password)
    finally:
        del password
    assert 'password_dpapi' in json.loads(path.read_bytes())['accounts']['banniu']
    assert not (ROOT/'data/sessions/banniu.dpapi').exists()
    print('Saved BN credentials with Windows DPAPI; copied only JST account/session. Source files unchanged.')

if __name__=='__main__': main()
