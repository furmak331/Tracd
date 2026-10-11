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


def test_data_from_the_old_name_is_kept(monkeypatch, tmp_path):
    monkeypatch.delenv("TRACD_HOME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    (tmp_path / ".trackyourfavs").mkdir()
    (tmp_path / ".trackyourfavs" / "trackyourfavs.db").write_text("x")
    home = cli.data_dir()
    assert home == tmp_path / ".tracd"
    assert (home / "tracd.db").read_text() == "x" and not (tmp_path / ".trackyourfavs").exists()
