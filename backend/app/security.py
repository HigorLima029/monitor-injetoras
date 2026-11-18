"""Senhas (scrypt), TOTP (RFC 6238) e tokens de sessão — só stdlib."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _scrypt(pw: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN,
                          maxmem=128 * n * r * 2 + 1024 * 1024)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(pw, salt, _N, _R, _P)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(pw: str, stored: str) -> bool:
    try:
        tag, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if tag != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if n < 2 or n & (n - 1) or r < 1 or p < 1 or n > 2**20:
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        actual = _scrypt(pw, salt, n, r, p)
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(pw: str, role: str) -> str | None:
    minimo = 12 if role == "mae" else 8
    if len(pw) < minimo:
        return f"A senha precisa ter pelo menos {minimo} caracteres."
    return None


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_code(secret: str, at: float, digits: int = 6, step: int = 30) -> str:
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = struct.pack(">Q", int(at // step))
    mac = hmac.new(key, counter, hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    num = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(num).zfill(digits)


def verify_totp(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    code = (code or "").strip()
    if len(code) != 6 or not (code.isascii() and code.isdigit()):
        return False
    now = time.time() if at is None else at
    ok = False
    for i in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, now + i * 30), code):
            ok = True
    return ok


def otpauth_uri(secret: str, username: str, issuer: str = "Monitor de Injetoras") -> str:
    label = f"{quote(issuer, safe='')}:{quote(username, safe='')}"
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
            "&algorithm=SHA1&digits=6&period=30")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
