"""Per-profile OS credential vault. Never fall back to plaintext files."""
from __future__ import annotations
import ctypes as C
import hashlib
import json
import os
import sys
import uuid


class CredentialVault:
    def __init__(self, profile_path, endpoint):
        # A reset/recreated profile must not inherit the old account merely
        # because its directory has the same name. Only this nonsecret UUID
        # lives beside the collection; credentials remain in the OS vault.
        identity_path = os.path.join(profile_path, "ankiscape-session-owner")
        try:
            with open(identity_path, "x") as fh:
                os.chmod(identity_path, 0o600)
                fh.write(str(uuid.uuid4()))
        except FileExistsError:
            pass
        with open(identity_path) as fh:
            owner = str(uuid.UUID(fh.read().strip()))
        self.account = hashlib.sha256((os.path.realpath(profile_path) + '\n' + owner + '\n' + endpoint).encode()).hexdigest()
        self.service = 'AnkiScape Evolved'
        self.available = False
        if sys.platform == 'darwin':
            self.lib = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
            self.lib.SecKeychainFindGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.POINTER(C.c_uint32), C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)]
            self.lib.SecKeychainAddGenericPassword.argtypes = [C.c_void_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_char_p, C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
            self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p]
            self.lib.SecKeychainItemDelete.argtypes = [C.c_void_p]
            self.lib.SecKeychainItemFreeContent.argtypes = [C.c_void_p, C.c_void_p]
            self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            self.cf.CFRelease.argtypes = [C.c_void_p]
            self.available = True
        else:
            try:
                import keyring
                # Accept only system vault implementations, never file backends.
                backend = keyring.get_keyring()
                if type(backend).__module__.startswith(('keyring.backends.Windows', 'keyring.backends.SecretService', 'keyring.backends.kwallet')):
                    self.keyring = keyring
                    self.available = True
            except Exception:
                pass

    def _find(self):
        length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
        service, account = self.service.encode(), self.account.encode()
        status = self.lib.SecKeychainFindGenericPassword(None, len(service), service, len(account), account, C.byref(length), C.byref(data), C.byref(item))
        raw = None
        if status == 0:
            raw = C.string_at(data, length.value).decode()
            self.lib.SecKeychainItemFreeContent(None, data)
        return status, raw, item

    def read(self):
        if not self.available:
            return None
        try:
            if sys.platform == 'darwin':
                status, raw, item = self._find()
                if item:
                    self.cf.CFRelease(item)
            else:
                raw = self.keyring.get_password(self.service, self.account)
            data = json.loads(raw) if raw else None
            if isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ('access_token', 'refresh_token', 'user_id')):
                return data
        except Exception:
            pass
        return None

    def write(self, data):
        if not self.available:
            return False
        raw = json.dumps(data).encode()
        try:
            if sys.platform == 'darwin':
                status, old, item = self._find()
                try:
                    if status == 0:
                        status = self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
                    elif status == -25300:
                        service, account = self.service.encode(), self.account.encode()
                        status = self.lib.SecKeychainAddGenericPassword(None, len(service), service, len(account), account, len(raw), raw, None)
                    return status == 0
                finally:
                    if item:
                        self.cf.CFRelease(item)
            self.keyring.set_password(self.service, self.account, raw.decode())
            return True
        except Exception:
            return False

    def delete(self):
        if not self.available:
            # No vault means nothing was ever stored: write() refuses in this
            # same state (above) and read() returns None. So there is nothing
            # to clear and the honest answer is success. Returning False here
            # reported "local cleanup is incomplete" for a credential that was
            # never written -- deterministically, on every platform without a
            # system vault, which is what failed the account-lifecycle journey
            # on six of seven CI lanes.
            return True
        try:
            if sys.platform == 'darwin':
                status, raw, item = self._find()
                try:
                    return status == -25300 or (status == 0 and self.lib.SecKeychainItemDelete(item) == 0)
                finally:
                    if item:
                        self.cf.CFRelease(item)
            self.keyring.delete_password(self.service, self.account)
            return True
        except Exception:
            return False
