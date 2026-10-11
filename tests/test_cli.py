import sys

from trackyourfavs import cli


def test_parser_defaults():
    ap = cli.build_parser()
    o = ap.parse_args([])
    assert o.func is cli.cmd_run and o.port == 8723 and not o.demo
    o = ap.parse_args(["check", "https://x", "--size", "UK 9"])
    assert o.func is cli.cmd_check and o.size == "UK 9"


def test_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TYF_HOME", str(tmp_path / "h"))
    assert cli.data_dir() == tmp_path / "h"
    assert cli.data_dir(str(tmp_path / "o")) == tmp_path / "o"


def test_autostart_on_off(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    ap = cli.build_parser()
    assert cli.cmd_autostart(ap.parse_args(["autostart", "on"])) == 0
    f = tmp_path / ".config" / "autostart" / "trackyourfavs.desktop"
    assert "-m trackyourfavs --no-open --quiet" in f.read_text()
    assert cli.cmd_autostart(ap.parse_args(["autostart", "off"])) == 0
    assert not f.exists()


def test_windows_autostart_script(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path, body = cli._startup_target()
    assert path.name == "TrackYourFavs.vbs" and "Startup" in str(path)
    assert "-m trackyourfavs --no-open --quiet" in body and ", 0, False" in body
