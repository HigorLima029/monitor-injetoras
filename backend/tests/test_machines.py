import json
from types import SimpleNamespace

import pytest

from app.machines import Machine, MachineStore, seed_from_settings


@pytest.fixture
def store(tmp_path):
    s = MachineStore(str(tmp_path / "cfg.db"))
    yield s
    s.close()


def test_crud_e_ativa(store):
    a = store.add(Machine(None, "A", "chy", "1.1.1.1"))
    b = store.add(Machine(None, "B", "em63", "2.2.2.2"))
    assert len(store.list()) == 2
    store.set_active(b.id)
    assert store.active_id() == b.id
    assert store.update(a.id, {"name": "X"}).name == "X"
    store.delete(a.id)
    assert store.get(a.id) is None


def test_senha_vazia_mantem(store):
    m = store.add(Machine(None, "A", "em63", "1.1.1.1", vnc_view_password="v1"))
    store.update(m.id, {"vnc_view_password": ""})
    assert store.get(m.id).vnc_view_password == "v1"
    store.update(m.id, {"vnc_view_password": "v2"})
    assert store.get(m.id).vnc_view_password == "v2"


def test_public_sem_senha():
    m = Machine(1, "A", "em63", "1.1.1.1", vnc_view_password="v1")
    pub = m.public()
    assert "vnc_view_password" not in pub
    assert "vnc_control_password" not in pub
    assert pub["has_view_password"] is True
    assert pub["has_control_password"] is False
    assert "v1" not in json.dumps(pub)


def test_padroes_por_tipo():
    pub = Machine(None, "B&R", "em63", "10.86.20.253").public()
    assert pub["namespace"] == 6
    assert pub["vnc_enabled"] is True
    assert pub["profile"] == "em63"
    assert pub["opcua_url"] == "opc.tcp://10.86.20.253:4840"
    ads = Machine(None, "P", "ads", "1.1.1.1")
    assert ads.status_port == 48898
    assert ads.public()["vnc_enabled"] is False
    assert Machine(None, "D", "demo", "x").profile == "chy"


def test_validacao(store):
    for m in (
        Machine(None, "x", "plc", "1.2.3.4"),
        Machine(None, "x", "chy", ""),
        Machine(None, "x", "chy", "1.2.3.4", opcua_port=70000),
    ):
        with pytest.raises(ValueError):
            store.add(m)


def test_seed_do_env(tmp_path, store):
    (tmp_path / "monitor.db").write_bytes(b"x")
    (tmp_path / "tags_cache.json").write_text("{}")
    st = SimpleNamespace(
        opcua_url="opc.tcp://10.86.20.253:4840", profile="em63", machine_name="JM228",
        vnc_view_password="v", vnc_control_password="", vnc_host="", vnc_port=5900,
        simulate=False, db_path=str(tmp_path / "monitor.db"), namespace=1,
    )
    mdir = str(tmp_path / "machines")
    seed_from_settings(store, st, mdir)
    seed_from_settings(store, st, mdir)
    ms = store.list()
    assert len(ms) == 1
    assert ms[0].kind == "em63" and ms[0].host == "10.86.20.253"
    assert store.active_id() == ms[0].id
    assert (tmp_path / "machines" / "1.db").read_bytes() == b"x"
    assert (tmp_path / "machines" / "1_tags.json").read_text() == "{}"


def _settings(tmp_path, **kw):
    base = dict(
        opcua_url="opc.tcp://10.0.0.5:4840", profile="chy", machine_name="M",
        vnc_view_password="", vnc_control_password="", vnc_host="", vnc_port=5900,
        simulate=False, db_path=str(tmp_path / "monitor.db"), namespace=1,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_delete_limpa_ativa(store):
    a = store.add(Machine(None, "A", "chy", "1.1.1.1"))
    store.set_active(a.id)
    store.delete(a.id)
    assert store.active_id() is None


def test_set_active_desconhecida(store):
    with pytest.raises(KeyError):
        store.set_active(999)
    assert store.active_id() is None


def test_update_invalido(store):
    with pytest.raises(KeyError):
        store.update(999, {"name": "x"})
    a = store.add(Machine(None, "A", "chy", "1.1.1.1"))
    with pytest.raises(ValueError):
        store.update(a.id, {"kind": "plc"})
    with pytest.raises(ValueError):
        store.update(a.id, {"opcua_port": 0})
    assert store.get(a.id).kind == "chy"


def test_seed_simulate_demo(tmp_path, store):
    seed_from_settings(store, _settings(tmp_path, simulate=True), str(tmp_path / "m"))
    assert store.list()[0].kind == "demo"


def test_seed_namespace_customizado(tmp_path, store):
    seed_from_settings(store, _settings(tmp_path, namespace=3), str(tmp_path / "m"))
    assert store.list()[0].namespace == 3


def test_seed_vnc_host_diferente_vai_para_notes(tmp_path, store):
    seed_from_settings(store, _settings(tmp_path, vnc_host="10.9.9.9"), str(tmp_path / "m"))
    assert "10.9.9.9" in store.list()[0].notes
    s2 = MachineStore(str(tmp_path / "c2.db"))
    seed_from_settings(s2, _settings(tmp_path, vnc_host="10.0.0.5"), str(tmp_path / "m"))
    assert s2.list()[0].notes == ""
    s2.close()


def test_seed_repara_sem_ativa(tmp_path, store):
    a = store.add(Machine(None, "A", "chy", "1.1.1.1"))
    seed_from_settings(store, _settings(tmp_path), str(tmp_path / "m"))
    assert len(store.list()) == 1
    assert store.active_id() == a.id


def test_seed_copia_falha_nao_bloqueia(tmp_path, store):
    (tmp_path / "monitor.db").write_bytes(b"x")
    (tmp_path / "blocker").write_text("arquivo")  # machines_dir dentro de um arquivo: mkdir falha
    seed_from_settings(store, _settings(tmp_path), str(tmp_path / "blocker" / "sub"))
    assert len(store.list()) == 1
    assert store.active_id() is not None
