import tempfile
import unittest
from pathlib import Path
from evolved.session_store import ProfileSession
from evolved.credentials import CredentialVault
from evolved.accounts import set_new_password
from evolved.net import Endpoint
from evolved.ui.guide import guide_pages
from evolved.data import load_rules

class FakeVault:
    available=True
    data=None
    def read(self): return self.data
    def write(self,data): self.data=dict(data); return True
    def delete(self): self.data=None; return True

class UserRegressions(unittest.TestCase):
    def test_restart_rotation_logout_and_session_only(self):
        vault=FakeVault()
        first=ProfileSession(generation=1,vault=vault)
        first.session.set(access_token='a',refresh_token='r',user_id='u',username='tester')
        first.clear()
        self.assertFalse(first.logged_in)
        second=ProfileSession(generation=2,vault=vault)
        self.assertTrue(second.logged_in)
        self.assertEqual(second.username,'tester')
        second.session.set(access_token='a2',refresh_token='r2',user_id='u')
        self.assertEqual(vault.data['refresh_token'],'r2')
        second.session.clear()
        self.assertIsNone(vault.data)
        second.remember=False
        second.session.set(access_token='a',refresh_token='r',user_id='u')
        self.assertIsNone(vault.data)

    def test_credential_scope_changes_when_profile_recreated(self):
        with tempfile.TemporaryDirectory() as path:
            one=CredentialVault(path,'https://example.invalid')
            two=CredentialVault(path,'https://example.invalid')
            self.assertEqual(one.account,two.account)
            self.assertNotEqual(one.account,CredentialVault(path,'https://different.invalid').account)
            Path(path,'ankiscape-session-owner').unlink()
            self.assertNotEqual(one.account,CredentialVault(path,'https://example.invalid').account)

    def test_unavailable_vault_has_nothing_to_clear(self):
        # delete() used to return False whenever the vault was unavailable, so
        # every account deletion on a platform without a system vault reported
        # "local cleanup is incomplete" and left the window open for a retry
        # that was never needed. write() refuses in that same state, so nothing
        # was ever stored and success is the honest answer. The two assertions
        # together are the point: nothing is stored, therefore nothing to clear.
        with tempfile.TemporaryDirectory() as path:
            vault=CredentialVault(path,'https://example.invalid')
            vault.available=False
            self.assertFalse(vault.write({'access_token':'a','refresh_token':'b','user_id':'c','username':'d'}))
            self.assertIsNone(vault.read())
            self.assertTrue(vault.delete())

    def test_password_reset_uses_put(self):
        calls=[]
        def transport(*args,**kwargs): calls.append((args,kwargs)); return {}
        self.assertTrue(set_new_password(transport,Endpoint('https://example.invalid','pub'),access_token='a',new_password='password1').ok)
        self.assertEqual(calls[0][0][1],'/auth/v1/user')
        self.assertEqual(calls[0][1]['method'],'PUT')

    def test_guide_covers_every_resource_and_all_thresholds(self):
        rules=load_rules(); pages=guide_pages(rules)
        self.assertEqual(len(pages),13)
        for title,table in [('Mining','ores'),('Woodcutting','trees'),('Fishing','fish'),('Cooking','fish'),('Smithing','bars'),('Crafting','crafting')]:
            for resource in rules[table]: self.assertIn(resource['display'],pages[title])
        for threshold in rules['thresholds']: self.assertIn(f'{threshold:,}',pages['Level table'])
        credits=pages['About & credits']
        self.assertIn('oldschool.runescape.wiki',credits)
        self.assertIn('Jagex Ltd',credits)
        self.assertIn('fish/shrimp.png',credits)

class BackupPathRegressions(unittest.TestCase):
    def test_backup_identifier_cannot_escape_profile(self):
        from evolved.backup import export_backup, validate_backup
        for bad in ('../escape','/tmp/escape','a/b','a\\b','..'):
            bundle=export_backup(bad,{'operations':[],'observations':[]})
            with self.assertRaises(ValueError): validate_backup(bundle)
