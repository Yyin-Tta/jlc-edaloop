from __future__ import annotations

import pytest

from edaloop import __version__
from edaloop.cli import build_parser, main


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_subcommands_parse() -> None:
    parser = build_parser()
    assert parser.parse_args(["run", "req.md"]).command == "run"
    assert parser.parse_args(["ingest", "a.pdf", "b.pdf"]).pdf == ["a.pdf", "b.pdf"]
    assert parser.parse_args(["eval", "--subset", "w1-retrieval"]).subset == "w1-retrieval"
    assert parser.parse_args(["replay", "runs/run-x"]).audit_dir == "runs/run-x"
    assert parser.parse_args(["seed", "--db", "x.db"]).db == "x.db"
    assert parser.parse_args(["retrieve", "TP4056 充电", "--top-k", "3"]).top_k == 3


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    for cmd in ("run", "ingest", "eval", "replay"):
        assert cmd in out


def test_unknown_command_exits(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(["frobnicate"])
