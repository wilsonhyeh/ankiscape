"""Hosted account smoke using admin-generated codes; never sends email.
Run with SUPABASE_SERVICE_ROLE_KEY in the environment. Deletes its test user.
"""
import json
import os
import sys
import urllib.request
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolved import accounts
from evolved.auth import MemorySession
from evolved.net import post_json, Endpoint
from evolved.prod_config import SUPABASE_URL, SUPABASE_ANON_KEY


def main():
    key=os.environ['SUPABASE_SERVICE_ROLE_KEY']
    ep=Endpoint(SUPABASE_URL,SUPABASE_ANON_KEY)
    suffix=uuid.uuid4().hex[:12]
    email=f'qa-{suffix}@example.invalid'
    username='qa_'+suffix
    password=uuid.uuid4().hex
    new_password=uuid.uuid4().hex
    user_id=None
    def admin(method,path,data=None):
        req=urllib.request.Request(SUPABASE_URL+path,method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as resp:
            return json.loads(resp.read() or '{}')
    def check(name,condition):
        if not condition: raise RuntimeError(name+' failed')
        print('PASS '+name)
    try:
        signup=admin('POST','/auth/v1/admin/generate_link',{'type':'signup','email':email,
            'password':password,'data':{'username_norm':username,'username_display':username}})
        user_id=signup.get('user',signup)['id']
        props=signup.get('properties',signup)
        session=MemorySession()
        check('signup code verification (no email sent)',accounts.verify_code(post_json,ep,email=email,
            code=props['email_otp'],kind='signup',session=session).ok)
        check('email login',accounts.login_password(post_json,ep,email=email,password=password,session=session).ok)
        check('username login',accounts.login_username(post_json,ep,username=username,password=password,session=session).ok)
        import tempfile
        from evolved.credentials import CredentialVault
        from evolved.session_store import ProfileSession
        with tempfile.TemporaryDirectory() as profile:
            vault=CredentialVault(profile,SUPABASE_URL)
            try:
                first=ProfileSession(generation=1,vault=vault)
                first.session.set(access_token=session.access_token,refresh_token=session.refresh_token,
                                  user_id=session.user_id,username=session.username)
                first.clear()
                restarted=ProfileSession(generation=2,vault=vault)
                check('restart restores hosted account from Keychain',restarted.logged_in and restarted.user_id==session.user_id)
            finally:
                vault.delete()
        refreshed=post_json(ep,'/auth/v1/token?grant_type=refresh_token',{'refresh_token':session.refresh_token})
        check('refresh token restores session',bool(refreshed.get('access_token')))
        recovery=admin('POST','/auth/v1/admin/generate_link',{'type':'recovery','email':email})
        props=recovery.get('properties',recovery)
        check('recovery code verification (no email sent)',accounts.verify_code(post_json,ep,email=email,
            code=props['email_otp'],kind='recovery',session=session).ok)
        check('password update via PUT',accounts.set_new_password(post_json,ep,
            access_token=session.access_token,new_password=new_password).ok)
        check('old password rejected',not accounts.login_password(post_json,ep,email=email,password=password,session=MemorySession()).ok)
        check('new password accepted',accounts.login_username(post_json,ep,username=username,password=new_password,session=MemorySession()).ok)
    finally:
        if user_id:
            admin('DELETE','/auth/v1/admin/users/'+user_id)
            print('PASS test account deleted')
if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc)[:150])
        raise SystemExit(1)
