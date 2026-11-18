"""Comando de administração: cria e recupera a conta mãe (super-admin).

Uso: python -m app.admin criar-mae --usuario LOGIN --nome NOME
     python -m app.admin resetar-senha --usuario LOGIN
     python -m app.admin resetar-2fa --usuario LOGIN

A senha é sempre pedida no terminal (nunca por argumento de linha de comando)
e nunca é impressa.
"""
from __future__ import annotations

import argparse
import getpass as _getpass
import sys
import time

from .accounts import AccountStore
from .config import settings
from .security import new_totp_secret, otpauth_uri, password_problem, verify_totp


def _ask_password(getpass, role: str, out) -> str | None:
    pw = getpass("Senha: ")
    if pw != getpass("Repita a senha: "):
        print("As senhas não conferem.", file=out)
        return None
    problem = password_problem(pw, role)
    if problem:
        print(problem, file=out)
        return None
    return pw


def _confirm_totp(username: str, ask, out) -> str | None:
    """Mostra um segredo novo e só o devolve se o usuário provar que o cadastrou."""
    secret = new_totp_secret()
    print("Cadastre no app autenticador do celular (Google Authenticator, "
          "Microsoft Authenticator...).", file=out)
    print(f"Segredo: {secret}", file=out)
    print(f"Endereço otpauth: {otpauth_uri(secret, username)}", file=out)
    code = (ask("Digite o código de 6 dígitos mostrado no app: ") or "").strip()
    if not verify_totp(secret, code, time.time()):
        print("Código incorreto. Nada foi gravado.", file=out)
        return None
    return secret


def main(argv: list[str], *, getpass=_getpass.getpass, out=sys.stdout,
         store: AccountStore | None = None, ask=input) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("criar-mae", help="cria a conta mãe (só uma)")
    c.add_argument("--usuario", required=True)
    c.add_argument("--nome", required=True)
    for name, text in (("resetar-senha", "define uma nova senha e derruba as sessões"),
                       ("resetar-2fa", "gera um novo segredo do autenticador")):
        s = sub.add_parser(name, help=text)
        s.add_argument("--usuario", required=True)
    args = parser.parse_args(argv)

    own = store is None
    store = store or AccountStore(settings.config_db_path)
    try:
        if args.cmd == "criar-mae":
            if any(u.role == "mae" for u in store.list_users()):
                print("Já existe uma conta mãe. Use resetar-senha ou resetar-2fa.", file=out)
                return 1
            if store.get_by_username(args.usuario):
                print("Esse login já existe.", file=out)
                return 1
            pw = _ask_password(getpass, "mae", out)
            if pw is None:
                return 1
            secret = _confirm_totp(args.usuario, ask, out)
            if secret is None:
                return 1
            user = store.add_user(args.usuario, args.nome, "mae", pw)
            store.set_totp(user.id, secret)
            print(f"Conta mãe '{user.username}' criada.", file=out)
            return 0

        user = store.get_by_username(args.usuario)
        if user is None:
            print(f"Usuário '{args.usuario}' não encontrado.", file=out)
            return 1
        if args.cmd == "resetar-senha":
            pw = _ask_password(getpass, user.role, out)
            if pw is None:
                return 1
            store.set_password(user.id, pw)
            store.delete_user_sessions(user.id)
            print("Senha alterada; as sessões abertas foram encerradas.", file=out)
            return 0
        secret = _confirm_totp(user.username, ask, out)  # resetar-2fa
        if secret is None:
            return 1
        store.set_totp(user.id, secret)
        store.delete_user_sessions(user.id)
        print("Autenticador atualizado; as sessões abertas foram encerradas.", file=out)
        return 0
    finally:
        if own:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
