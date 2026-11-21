"""Rootless podman compatibility for the moulinette's validation (loaded only through PYTHONPATH by our scripts).

SWE-bench's ``copy_to_container`` puts a tar archive into the container; its entries carry the owner of the
file on the host (your UID, e.g. 44530196). A rootless podman cannot map that UID inside the container and
answers ``lchown … invalid argument``, so every validation fails before the tests run. ``ignore_chown_errors``
does not cover this copy. Here the entries are written as root (UID 0), which a rootless podman maps to you.

Active only when the Docker API is a podman socket (DOCKER_HOST contains "podman"); with Docker nothing
changes. File contents, paths and modes are untouched: only the owner recorded in the archive.
"""

import os
import tarfile

if "podman" in os.environ.get("DOCKER_HOST", ""):
    _gettarinfo = tarfile.TarFile.gettarinfo

    def _as_root(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        info = _gettarinfo(self, *args, **kwargs)
        if info is not None:
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
        return info

    tarfile.TarFile.gettarinfo = _as_root
