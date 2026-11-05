"""User credentials in Windows Credential Manager or a native keyring backend."""
import os
import sys
from .core import GBMError

TARGET = 'GBM/Groq/APIKey'


class CredentialStore:
    def __init__(self):
        if os.name == 'nt':
            import ctypes as c
            from ctypes import wintypes as w
            class Credential(c.Structure):
                _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                            ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME),
                            ('CredentialBlobSize', w.DWORD), ('CredentialBlob', c.POINTER(c.c_ubyte)),
                            ('Persist', w.DWORD), ('AttributeCount', w.DWORD), ('Attributes', c.c_void_p),
                            ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]
            self.c, self.Credential = c, Credential
            self.api = c.WinDLL('Advapi32.dll', use_last_error=True)
            self.api.CredReadW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.POINTER(c.POINTER(Credential))]
            self.api.CredReadW.restype = w.BOOL
            self.api.CredWriteW.argtypes = [c.POINTER(Credential),w.DWORD]
            self.api.CredWriteW.restype = w.BOOL
            self.api.CredDeleteW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD]
            self.api.CredDeleteW.restype = w.BOOL
            self.api.CredFree.argtypes = [c.c_void_p]
            self.api.CredFree.restype = None
        else:
            try:
                import keyring
                backend = keyring.get_keyring()
                allowed = ('keyring.backends.macOS',) if sys.platform == 'darwin' else (
                    'keyring.backends.SecretService', 'keyring.backends.kwallet')
                if type(backend).__module__ not in allowed:
                    raise GBMError('No supported native credential store. Configure a native keyring backend; plaintext storage is never used.')
                self.backend = backend
            except ImportError:
                raise GBMError('Secure setup needs keyring on this platform: python -m pip install keyring. Configure the native OS credential service, then retry.') from None

    def get(self):
        if os.name != 'nt':
            try: return self.backend.get_password(TARGET, 'groq')
            except Exception: raise GBMError('Could not unlock/read the native credential store.') from None
        c = self.c
        pointer = c.POINTER(self.Credential)()
        if not self.api.CredReadW(TARGET, 1, 0, c.byref(pointer)):
            code = c.get_last_error()
            if code == 1168: return None
            raise GBMError(f'Windows Credential Manager read failed ({code}).')
        try:
            entry = pointer.contents
            return c.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode('utf-8')
        finally: self.api.CredFree(pointer)

    def set(self, key):
        if os.name != 'nt':
            try: self.backend.set_password(TARGET, 'groq', key)
            except Exception: raise GBMError('Could not save to the native credential store.') from None
            return
        data = key.encode('utf-8')
        if len(data)>2500: raise GBMError('API key exceeds the credential size limit.')
        blob = (self.c.c_ubyte * len(data)).from_buffer_copy(data)
        entry = self.Credential(Type=1, TargetName=TARGET, CredentialBlobSize=len(data),
                                CredentialBlob=blob, Persist=2, UserName='groq')
        if not self.api.CredWriteW(self.c.byref(entry), 0):
            raise GBMError(f'Windows Credential Manager write failed ({self.c.get_last_error()}).')

    def delete(self):
        if os.name != 'nt':
            try: self.backend.delete_password(TARGET, 'groq')
            except Exception: raise GBMError('Could not remove the native credential.') from None
            return
        if not self.api.CredDeleteW(TARGET,1,0) and self.c.get_last_error()!=1168:
            raise GBMError('Windows Credential Manager delete failed.')
