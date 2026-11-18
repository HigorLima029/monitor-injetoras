"""Quem pode ver e fazer o quê, por perfil (`mae`, `tecnico`, `cliente`).

Regras num lugar só, para as rotas HTTP e os WebSockets decidirem igual:
- conta mãe e técnico veem todas as máquinas (inclusive as em preparação, sem cliente) e os
  dados de conexão (IP, portas, AMS Net ID, notas; nunca as senhas);
- cliente vê só as máquinas do próprio cliente, sem os dados de conexão;
- controlar a tela: nunca o cliente; máquina vinculada a um cliente só com a autorização dele.
"""
from __future__ import annotations

from .accounts import Grant, User
from .machines import Machine

STAFF = ("mae", "tecnico")
CLIENTE_SO_VE = "Cliente só visualiza a tela."
PRECISA_AUTORIZACAO = "Precisa da autorização do cliente."


def can_view(user: User, machine: Machine) -> bool:
    if user.role in STAFF:
        return True
    if user.role == "cliente":
        return user.client_id is not None and machine.client_id == user.client_id
    return False


def sees_connection(user: User) -> bool:
    return user.role in STAFF


def control_problem(user: User, machine: Machine, grant: Grant | None) -> str | None:
    """Por que o usuário não pode assumir o controle da tela (None = pode)."""
    if user.role not in STAFF:
        return CLIENTE_SO_VE
    if machine.client_id is not None and grant is None:
        return PRECISA_AUTORIZACAO
    return None


def machine_view(user: User, machine: Machine, status: dict, client_name: str, *,
                 as_client: bool = False) -> dict:
    """A máquina como o usuário pode vê-la (`as_client`: "ver como o cliente")."""
    if as_client or not sees_connection(user):
        return {
            "id": machine.id,
            "name": machine.name,
            "kind": machine.kind,
            "profile": machine.profile,
            "client_id": machine.client_id,
            "client_name": client_name,
            "status": status,
            "vnc": machine.effective_vnc_enabled and machine.kind != "demo",
        }
    return {**machine.public(), "client_name": client_name, "status": status}
