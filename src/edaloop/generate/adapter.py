from __future__ import annotations

import subprocess
from pathlib import Path

_PINNED_VERSION = "1.2.10"  # ADR-0002→ADR-0011→1.2.10:0.25.1→1.1.1→1.2.8→1.2.10(2026-08-28 连接器平台侧自动升级,CLI 同步跟升;双机轮换开发,另一台同步升级后同过此门)
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
        self._project = project or os.environ.get("EDALOOP_PROJECT", "")
        self._window = os.environ.get("EDALOOP_WINDOW", "")
        self._window_resolved = bool(self._window)

    def _resolve_window(self) -> None:
        """同工程多窗口:按 windowId 稳定排序,探活(sch pages)择第一个健康窗口并粘滞。
        lastSeen 排序会随心跳翻转导致进程间打到不同窗口,禁止用于选择。"""
        self._window_resolved = True
        if self._window or not self._project:
            return
        try:
            health = self.daemon_health()
        except AdapterError:
            return
        cands = sorted(
            (w.get("windowId", "") for w in health.get("found", {}).get("raw", {}).get("windows", [])
             if (w.get("context", {}) or {}).get("projectName") == self._project)
        )
        for wid in cands:
            try:
                rc, _, _ = self._runner(["sch", "pages", "--window", wid])
                if rc == 0:
                    self._window = wid
                    return
            except Exception:
                continue

    def project_windows(self) -> list[str]:
        try:
            health = self.daemon_health()
        except AdapterError:
            return []
        return sorted(
            w.get("windowId", "")
            for w in health.get("found", {}).get("raw", {}).get("windows", [])
            if (w.get("context", {}) or {}).get("projectName") == self._project
        )

    def clear_all_pages(self) -> None:
        """清空所有同工程窗口的页面(双开窗口残留互访会污染布局校验)。"""
        for wid in self.project_windows():
            try:
                self._runner(["sch", "clear", "--window", wid])
            except Exception:
                continue

    def refresh_window(self) -> None:
        """连接器抖动后重新解析窗口(丢弃旧钉扎)。"""
        self._window = ""
        self._window_resolved = False
        self._resolve_window()

    @property
    def window_id(self) -> str:
        if not self._window_resolved:
            self._resolve_window()
        return self._window

    def _pinned(self, args: list[str]) -> list[str]:
        if args and args[0] not in ("sch", "pcb", "lib"):
            return args
        if not self._window_resolved:
            self._resolve_window()
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
        """跑一条 easyeda CLI 命令,600s 超时**杀进程树**。

        为什么不用 subprocess.run(timeout=):真机 freeze-pack 实测(2026-08-31
        run-955eb4729cff)超时只 TerminateProcess 直子进程,管道写端被孙进程
        继承时 communicate() 永远等不到 EOF——run 在 designator-rename 后冻死
        38min 无任何审计(py-spy 栈停在 _communicate join)。连接器 wedge 的
        项目侧次责项之一(见 docs 连接器假死定性):超时必须 taskkill /T /F
        树杀,让 TimeoutExpired 真的抛出来,调用方走既有失败/重试路径。
        """
        proc = subprocess.Popen(
            [self._bin, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            out, err = proc.communicate(timeout=600)
            return proc.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            self._kill_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=15)  # 树杀后管道收尾
            except subprocess.TimeoutExpired:  # 还有野句柄:放弃等待,别再挂死
                out, err = "", ""
            raise AdapterError(
                f"easyeda {' '.join(args[:3])} 超时(600s,连接器 wedge?已树杀;"
                f"stderr 尾: {(err or '')[-300:]}"
            ) from None

    @staticmethod
    def _kill_tree(pid: int) -> None:
        import os
        import sys

        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        else:
            import signal

            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

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
