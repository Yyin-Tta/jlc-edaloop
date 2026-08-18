from __future__ import annotations

import subprocess
from pathlib import Path

_PINNED_VERSION = "0.25.1"
_FALLBACK_BIN = r"C:\Users\admin\.local\bin\easyeda.exe"


class AdapterError(Exception):
    pass


class GateResult:
    def __init__(self, verdict: str, report: dict) -> None:
        self.verdict = verdict
        self.report = report

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


class EasyedaAdapter:
    def __init__(self, bin_path: str | None = None, runner=None, project: str | None = None) -> None:
        import os

        self._bin = bin_path or self._discover()
        self._runner = runner or self._subprocess_run
        self._window = os.environ.get("EDALOOP_WINDOW", "")
        self._project = "" if self._window else (project or os.environ.get("EDALOOP_PROJECT", ""))

    def _pinned(self, args: list[str]) -> list[str]:
        if args and args[0] not in ("sch", "pcb"):
            return args
        if self._window and "--window" not in args:
            return [*args, "--window", self._window]
        if self._project and "--project" not in args and "--window" not in args:
            return [*args, "--project", self._project]
        return args

    @staticmethod
    def _discover() -> str:
        import os

        env = os.environ.get("EASYEDA_BIN")
        if env and Path(env).exists():
            return env
        if Path(_FALLBACK_BIN).exists():
            return _FALLBACK_BIN
        return "easyeda"

    def _subprocess_run(self, args: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run(
            [self._bin, *args], capture_output=True, text=True, encoding="utf-8", timeout=600
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def run(self, args: list[str]) -> tuple[int, str, str]:
        return self._runner(self._pinned(args))

    def check_version(self) -> str:
        rc, out, _ = self.run(["version"])
        if rc != 0:
            raise AdapterError("easyeda version 命令失败")
        m = out.strip().split()[-1].lstrip("v")
        if m != _PINNED_VERSION:
            raise AdapterError(
                f"easyeda-agent 版本 {m} 与钉死版本 {_PINNED_VERSION} 不一致(ADR-0002)"
            )
        return m

    def daemon_health(self) -> dict:
        import json

        rc, out, _ = self.run(["daemon", "health"])
        if rc != 0:
            raise AdapterError("daemon health 失败")
        data = json.loads(out)
        if data.get("status") != "found":
            raise AdapterError(f"daemon 未找到: {data.get('status')}")
        return data

    def run_json(self, args: list[str]) -> dict:
        import json

        rc, out, err = self.run(args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise AdapterError(
                f"JSON 解析失败(rc={rc}): {e}\n"
                f"stdout(len={len(out)})={out[-1500:] if len(out) < 1500 else out[:1500]}\n"
                f"stderr(len={len(err)})={err[-3000:]}"
            ) from e

    def apply_and_gate(self, actions: list) -> list[dict]:
        results = []
        for act in actions:
            if act.kind == "sch-gate":
                report = self.run_json(act.args)
                verdict = report.get("verdict", "unknown")
                results.append({"kind": "gate", "verdict": verdict, "report": report})
            else:
                manifest = self.run_json(act.args)
                status = manifest.get("ok") or manifest.get("status") or "unknown"
                results.append(
                    {"kind": "block-apply", "instance": act.block_instance, "status": status, "manifest": manifest}
                )
        return results

    def delete_primitives(self, ids: list[str]) -> dict:
        return self.run_json(["sch", "prim-delete", "--ids", ",".join(ids)])
