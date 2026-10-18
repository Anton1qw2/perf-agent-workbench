"""Backend-only encrypted token storage. Tokens never belong to task or LLM contracts."""

import base64
import ctypes
import os


def _dpapi(data: bytes, *, decrypt=False, machine=False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    method.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(
        ctypes.byref(incoming),
        None,
        None,
        None,
        None,
        1 | (4 if machine and not decrypt else 0),
        ctypes.byref(outgoing),
    ):
        raise ValueError("Token encryption is unavailable for this OS account")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def _fernet():
    try:
        from cryptography.fernet import Fernet

        return Fernet(os.environ["PERF_CHAT_SECRET_KEY"].encode("ascii"))
    except (ImportError, KeyError, ValueError) as exc:
        raise ValueError(
            "Configure PERF_CHAT_SECRET_KEY and install perf-chat[secrets] for token storage"
        ) from exc


def protect(token: str) -> str:
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(token.encode())).decode("ascii")
        except ValueError:
            # Service accounts may not have a loaded user profile. Database ACLs then
            # provide user separation for Windows machine-scoped protected data.
            return "dpapi-machine:" + base64.b64encode(_dpapi(token.encode(), machine=True)).decode(
                "ascii"
            )
    return "fernet:" + _fernet().encrypt(token.encode()).decode("ascii")


def unprotect(value: str) -> str:
    if value.startswith(("dpapi:", "dpapi-machine:")) and os.name == "nt":
        return _dpapi(base64.b64decode(value.split(":", 1)[1]), decrypt=True).decode()
    if value.startswith("fernet:"):
        return _fernet().decrypt(value[7:].encode()).decode()
    raise ValueError("Token cannot be decrypted by this backend")
