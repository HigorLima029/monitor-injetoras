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
    # leitura rápida enquanto alguém olha a máquina (dados ou tela)
    poll_seconds: float = float(os.getenv("POLL_SECONDS", "2"))
    # leitura sem ninguém olhando (economiza o 4G) e quanto tempo depois do último sair volta a ela
    idle_poll_seconds: float = float(os.getenv("IDLE_POLL_SECONDS", "10"))
    viewer_linger_seconds: float = float(os.getenv("VIEWER_LINGER_SECONDS", "60"))
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
    vnc_audit_path: str | None = _path(os.getenv("VNC_AUDIT_PATH", "data/vnc_audit.log"))
    # Cadastro de maquinas (SQLite) e historico/cache de tags por maquina
    config_db_path: str = _path(os.getenv("CONFIG_DB_PATH", "data/monitor_config.db"))
    machines_dir: str = _path(os.getenv("MACHINES_DIR", "data/machines"))
    # AMS Net ID local do servidor (rota ADS); vazio = nao configurado
    ads_local_net_id: str = os.getenv("ADS_LOCAL_NET_ID", "")
    frontend_dist: str = _path(os.getenv("FRONTEND_DIST", "frontend/dist"))
    # Tela ao vivo da Beckhoff: CERHOST.exe rodando no Wine numa tela virtual (Xvfb :N).
    # Vazio = tela da Beckhoff desligada (com aviso); os dados ADS seguem normais.
    cerhost_exe: str = _path(os.getenv("CERHOST_EXE", "")) if os.getenv("CERHOST_EXE", "").strip() else ""
    # primeira tela virtual; cada ponte viva usa a menor livre a partir dela (:99, :100, ...)
    cerhost_display: int = int(os.getenv("CERHOST_DISPLAY", "99"))
    # telas da Beckhoff abertas ao mesmo tempo (cada uma é um Wine + Xvfb + x11vnc)
    cerhost_max_screens: int = int(os.getenv("CERHOST_MAX_SCREENS", "2"))
    # tela ao vivo sem ninguém por esse tempo fecha (Beckhoff: ponte encerrada, tela liberada)
    screen_idle_seconds: float = float(os.getenv("SCREEN_IDLE_SECONDS", "120"))
    # Prefixo Wine DEDICADO ao CERHost (opcional). Com ele, ao parar a ponte roda `wineserver -k`
    # só nesse prefixo. Vazio = usa o prefixo herdado e nunca roda `wineserver -k`
    # (mataria outros programas Wine do mesmo usuário).
    cerhost_wineprefix: str = (_path(os.getenv("CERHOST_WINEPREFIX", ""))
                               if os.getenv("CERHOST_WINEPREFIX", "").strip() else "")
    # pids dos processos da ponte: a proxima subida encerra as sobras de um Monitor que caiu feio.
    # Vale a pasta: cada tela virtual tem o seu arquivo (data/cerhost_99.pids, ...)
    cerhost_pidfile: str = _path(os.getenv("CERHOST_PIDFILE", "data/cerhost.pids"))
    # Login: cookie de sessão só por HTTPS (desligar só em teste local sem HTTPS)
    cookie_secure: bool = _bool("COOKIE_SECURE", True)
    session_idle_hours: float = float(os.getenv("SESSION_IDLE_HOURS", "8"))
    session_max_hours: float = float(os.getenv("SESSION_MAX_HOURS", "24"))


settings = Settings()
