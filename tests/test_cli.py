import sys

from tracd import cli


def test_parser_defaults():
    ap = cli.build_parser()
    o = ap.parse_args([])
    assert o.func is cli.cmd_run and o.port == 8723 and not o.demo
    o = ap.parse_args(["check", "https://x", "--size", "UK 9"])
    assert o.func is cli.cmd_check and o.size == "UK 9"


def test_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACD_HOME", str(tmp_path / "h"))
    assert cli.data_dir() == tmp_path / "h"
    assert cli.data_dir(str(tmp_path / "o")) == tmp_path / "o"


def test_autostart_on_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    ap = cli.build_parser()
    assert cli.cmd_autostart(ap.parse_args(["autostart", "on"])) == 0
    f = tmp_path / ".config" / "autostart" / "tracd.desktop"
    assert "-m tracd --no-open --quiet" in f.read_text()
    assert cli.cmd_autostart(ap.parse_args(["autostart", "off"])) == 0
    assert not f.exists()


def test_windows_autostart_script(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path, body = cli._startup_target()
    assert path.name == "Tracd.vbs" and "Startup" in str(path)
    assert "-m tracd --no-open --quiet" in body and ", 0, False" in body


def _old_db(path):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE w (x)")
    con.execute("INSERT INTO w VALUES ('kept')")
    con.commit()
    return con  # left open, so the last change is still only in the -wal file


def _rows(path):
    import sqlite3

    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT x FROM w")]
    finally:
        con.close()


def test_data_from_the_old_name_is_kept(monkeypatch, tmp_path):
    monkeypatch.delenv("TRACD_HOME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    (tmp_path / ".trackyourfavs").mkdir()
    con = _old_db(tmp_path / ".trackyourfavs" / "trackyourfavs.db")
    assert (tmp_path / ".trackyourfavs" / "trackyourfavs.db-wal").stat().st_size > 0
    home = cli.data_dir()
    con.close()
    assert home == tmp_path / ".tracd"
    assert _rows(home / "tracd.db") == ["kept"] and not (tmp_path / ".trackyourfavs").exists()


def test_old_data_is_adopted_even_if_the_new_folder_was_already_made(monkeypatch, tmp_path):
    monkeypatch.delenv("TRACD_HOME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    (tmp_path / ".trackyourfavs").mkdir()
    _old_db(tmp_path / ".trackyourfavs" / "trackyourfavs.db").close()
    (tmp_path / ".tracd").mkdir()
    (tmp_path / ".tracd" / "tracd.log").write_text("log")
    home = cli.data_dir()
    assert _rows(home / "tracd.db") == ["kept"]
    assert (home / "tracd.log").read_text() == "log"


def test_port_is_free_again_right_after_a_restart():
    import socket

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    c = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # the server side closes first, leaving the port in TIME_WAIT
    c.close()
    srv.close()
    assert cli.port_free("127.0.0.1", port)
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        assert not cli.port_free("127.0.0.1", busy.getsockname()[1])
    finally:
        busy.close()
