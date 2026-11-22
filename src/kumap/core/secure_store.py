"""本地凭证安全存储。

Windows：使用系统 DPAPI（crypt32 CryptProtectData，绑定当前用户 + 附加应用熵），
密文落盘，密钥由系统管理，不写入任何明文口令。
非 Windows：退化为 PBKDF2 派生密钥 + Fernet，仅用于开发调试。

存储内容仅为库街区官方授权回调返回的 access/refresh token 与脱敏 uid；
工具从不接触、也不请求用户的账号密码。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import secrets
from pathlib import Path

from . import paths

_APP_ENTROPY = b"KuroMapHelper.v1:kuro-bbs-oauth-token"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


_win32_api: tuple | None = None


def _dpapi_available() -> bool:
    return platform.system() == "Windows"


def _get_win32_api():
    """延迟绑定 crypt32 / kernel32，并声明完整签名。

    不声明 argtypes/restype 时，ctypes 默认把指针按 C int 传参：64 位系统上
    LocalFree 释放高位地址会抛 OverflowError（令牌写入直接失败），
    CryptProtectData 的 DATA_BLOB 指针也有同样的截断风险。
    """
    global _win32_api
    if _win32_api is None:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_p = ctypes.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_p, ctypes.c_wchar_p, blob_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, blob_p,
        ]
        crypt32.CryptProtectData.restype = ctypes.c_bool
        crypt32.CryptUnprotectData.argtypes = [
            blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, blob_p,
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _win32_api = (crypt32, kernel32)
    return _win32_api


def _blob_of(buf: bytes) -> tuple[_DataBlob, ctypes.Array]:
    """构造 DATA_BLOB，并返回其底层缓冲区（调用方须保持缓冲区存活）。"""
    arr = ctypes.create_string_buffer(buf, len(buf))
    return _DataBlob(len(buf), ctypes.cast(arr, ctypes.c_void_p)), arr


def _dpapi_protect(data: bytes) -> bytes:
    crypt32, kernel32 = _get_win32_api()
    blob, _src = _blob_of(data)
    salt, _salt_buf = _blob_of(_APP_ENTROPY)
    out = _DataBlob()
    ok = crypt32.CryptProtectData(ctypes.byref(blob), "KuroMapHelper", ctypes.byref(salt), None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32, kernel32 = _get_win32_api()
    blob, _src = _blob_of(data)
    salt, _salt_buf = _blob_of(_APP_ENTROPY)
    out = _DataBlob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob), None, ctypes.byref(salt), None, None, 0x01, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _fallback_key(path: Path) -> bytes:
    """非 Windows 开发用密钥：与本机标识绑定，密钥文件权限 0600。"""
    key_path = path.with_suffix(".key")
    if key_path.exists():
        raw = key_path.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    secret = secrets.token_bytes(32)
    machine = f"{platform.node()}|{os.environ.get('USER', 'local')}".encode()
    derived = hashlib.pbkdf2_hmac("sha256", secret, machine, 200_000, dklen=32)
    key_path.write_bytes(derived)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return derived


class SecureStore:
    """一个加密的 key-value 容器。"""

    def __init__(self, path: Path | None = None):
        self.path = path or paths.secure_store_path()
        self._cache: dict | None = None

    # --- 底层加解密 ---
    def _encrypt(self, data: bytes) -> tuple[str, bytes]:
        if _dpapi_available():
            return "dpapi", _dpapi_protect(data)
        from cryptography.fernet import Fernet

        f = Fernet(base64.urlsafe_b64encode(_fallback_key(self.path)))
        return "fernet", f.encrypt(data)

    def _decrypt(self, scheme: str, data: bytes) -> bytes:
        if scheme == "dpapi":
            return _dpapi_unprotect(data)
        from cryptography.fernet import Fernet

        f = Fernet(base64.urlsafe_b64encode(_fallback_key(self.path)))
        return f.decrypt(data)

    # --- 对外接口 ---
    def read(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {}
            return self._cache
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            raw = self._decrypt(envelope["scheme"], base64.b64decode(envelope["blob"]))
            self._cache = json.loads(raw.decode("utf-8"))
        except Exception:
            # 密文损坏或换了系统用户：直接视为未登录，绝不明文回退
            self._cache = {}
        return self._cache

    def write(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        scheme, blob = self._encrypt(data)
        envelope = {"scheme": scheme, "blob": base64.b64encode(blob).decode("ascii")}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._cache = payload

    def get(self, key: str, default=None):
        return self.read().get(key, default)

    def clear(self) -> None:
        """退出登录：本地凭证立即清除。"""
        self._cache = {}
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    def is_empty(self) -> bool:
        return not self.read()
