"""Configuração lida de variáveis de ambiente (ou do arquivo .env na raiz)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "sim", "yes", "on"}


def _path(raw: str) -> str:
    """Caminhos relativos valem a partir da raiz do projeto, não da pasta atual."""
    if raw == ":memory:":
        return raw
    p = Path(raw)
    return str(p if p.is_absolute() else ROOT / p)


@dataclass(frozen=True)
class Settings:
    opcua_url: str = os.getenv("OPCUA_URL", "opc.tcp://10.202.148.102:4840")
    machine_name: str = os.getenv("MACHINE_NAME", "Injetora Chen Hsong (bancada)")
    poll_seconds: float = float(os.getenv("POLL_SECONDS", "5"))
    full_poll_seconds: float = float(os.getenv("FULL_POLL_SECONDS", "60"))
    simulate: bool = _bool("SIMULATE", False)
    db_path: str = _path(os.getenv("DB_PATH", "data/monitor.db"))
    retention_days: int = int(os.getenv("RETENTION_DAYS", "7"))
    namespace: int = int(os.getenv("OPCUA_NAMESPACE", "1"))
    # "chy" = gateway Mega CHL3200 (variaveis ns=1;s=Nome) | "em63" = painel B&R Euromap 63
    profile: str = os.getenv("MACHINE_PROFILE", "chy").strip().lower()
    # Tela ao vivo (VNC do painel). Host vazio = o mesmo do OPCUA_URL.
    vnc_enabled: bool = _bool("VNC_ENABLED", os.getenv("MACHINE_PROFILE", "chy").strip().lower() == "em63")
    vnc_host: str = os.getenv("VNC_HOST", "")
    vnc_port: int = int(os.getenv("VNC_PORT", "5900"))
    # As duas senhas VNC do painel: a de visualizacao so assiste; a de operacao pode assumir o controle.
    # Ficam so no servidor (.env); o navegador nunca recebe. Operacao vazia = ninguem opera.
    vnc_view_password: str = os.getenv("VNC_VIEW_PASSWORD", "")
    vnc_control_password: str = os.getenv("VNC_CONTROL_PASSWORD", "")
    # segundos sem mexer ate o operador perder o controle
    vnc_control_timeout: float = float(os.getenv("VNC_CONTROL_TIMEOUT", "300"))
    # porta VNC comum do hub, para o Espelho IHM ou outro cliente VNC (0 = desligada)
    vnc_tcp_port: int = int(os.getenv("VNC_TCP_PORT", "5901"))
    vnc_tcp_bind: str = os.getenv("VNC_TCP_BIND", "0.0.0.0")
    vnc_audit_path: str | None = _path(os.getenv("VNC_AUDIT_PATH", "data/vnc_audit.log"))
    frontend_dist: str = _path(os.getenv("FRONTEND_DIST", "frontend/dist"))


settings = Settings()
