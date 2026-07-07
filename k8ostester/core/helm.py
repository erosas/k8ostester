"""Thin helm CLI wrapper. Charts are the delivery mechanism for infra
prerequisites (operators, SeaweedFS, monitoring); we shell out rather than
reimplement chart rendering."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class HelmError(Exception):
    pass


from k8ostester.core.exceptions import K8osInfraError


class Helm:
    def __init__(self, context: str | None = None):
        from k8ostester.core.k8s import ClusterClient
        self.helm = ClusterClient(context)._check_helm()
        self.context = context

    def _run(self, *args: str, timeout: int = 600) -> str:
        cmd = [self.helm, *args]
        if self.context:
            cmd += ["--kube-context", self.context]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise HelmError(f"{' '.join(cmd)} failed:\n{result.stderr.strip()}")
        return result.stdout

    def repo_add(self, name: str, url: str) -> None:
        # --force-update makes re-adding an existing repo a no-op
        self._run("repo", "add", name, url, "--force-update")
        self._run("repo", "update", name)

    def upgrade_install(
        self,
        release: str,
        chart: str,
        namespace: str,
        version: str | None = None,
        values_file: Path | None = None,
        set_values: dict[str, str] | None = None,
        wait: bool = True,
    ) -> None:
        args = [
            "upgrade", "--install", release, chart,
            "--namespace", namespace, "--create-namespace",
        ]
        if version:
            args += ["--version", version]
        if values_file:
            args += ["--values", str(values_file)]
        for key, value in (set_values or {}).items():
            args += ["--set", f"{key}={value}"]
        if wait:
            args += ["--wait", "--timeout", "10m"]
        self._run(*args)

    def uninstall(self, release: str, namespace: str) -> None:
        self._run("uninstall", release, "--namespace", namespace)

    def release_exists(self, release: str, namespace: str) -> bool:
        out = self._run("list", "--namespace", namespace, "--short")
        return release in out.splitlines()
