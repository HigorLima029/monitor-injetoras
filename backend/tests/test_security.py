import base64

from app.security import (
    hash_password, verify_password, password_problem, totp_code, verify_totp,
    otpauth_uri, new_totp_secret, new_token, token_hash,
)


def test_hash_e_verificacao():
    h = hash_password("senha-forte-123")
    assert h.startswith("scrypt$") and "senha-forte-123" not in h
    assert verify_password("senha-forte-123", h) and not verify_password("outra", h)
    assert hash_password("x" * 12) != hash_password("x" * 12)   # sal aleatório
    assert verify_password("x", "lixo") is False


def test_politica_de_senha():
    assert password_problem("curta123", "mae") and password_problem("curta123", "tecnico") is None


def test_totp_vetor_rfc6238():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_code(secret, 59) == "287082"
    assert verify_totp(secret, "287082", at=59 + 30)          # janela de 1 passo
    assert not verify_totp(secret, "287082", at=59 + 90)


def test_totp_entradas_invalidas():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert verify_totp(secret, " 287082 ", at=59)
    assert not verify_totp(secret, "28708", at=59)
    assert not verify_totp(secret, "28708a", at=59)


def test_uri_otpauth():
    assert otpauth_uri("ABC", "higor").startswith("otpauth://totp/Monitor%20de%20Injetoras:higor?secret=ABC")
    assert otpauth_uri("ABC", "higor").endswith(
        "&issuer=Monitor%20de%20Injetoras&algorithm=SHA1&digits=6&period=30")


def test_segredo_e_tokens():
    assert len(base64.b32decode(new_totp_secret())) == 20
    t = new_token()
    assert t != new_token() and len(token_hash(t)) == 64 and token_hash(t) == token_hash(t)
