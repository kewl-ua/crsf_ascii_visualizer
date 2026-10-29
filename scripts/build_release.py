#!/usr/bin/env python3
"""Build Python distributions and an architecture-independent Debian package."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "crsf-ascii-visualizer"


def command(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, check=True, text=True, **kwargs)


def version() -> str:
    source = (ROOT / "crsf_ascii_visualizer" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
    if not match:
        raise SystemExit("Expected a numeric MAJOR.MINOR.PATCH package version.")
    return match.group(1)


def build_deb(out: Path, release_version: str, maintainer: str | None) -> Path:
    if sys.platform != "linux" or not shutil.which("dpkg-deb"):
        raise SystemExit("Debian builds need Linux and dpkg-deb; use --skip-deb for Python distributions only.")
    if not maintainer:
        maintainer = command("git", "show", "-s", "--format=%an <%ae>", "HEAD", capture_output=True).stdout.strip()
    if "\n" in maintainer or not re.fullmatch(r"[^<>]+ <[^<>\s]+@[^<>\s]+>", maintainer):
        raise SystemExit("Provide --maintainer 'Name <email>' for the Debian control file.")
    with tempfile.TemporaryDirectory(prefix="crsf-deb-") as directory:
        stage = Path(directory) / "stage"
        command("make", "install", "PREFIX=/usr", f"DESTDIR={stage}", "PYTHON=/usr/bin/python3")
        control = stage / "DEBIAN"
        control.mkdir()
        size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
        (control / "control").write_text(
            f"Package: {PACKAGE}\n"
            f"Version: {release_version}\n"
            "Section: utils\n"
            "Priority: optional\n"
            "Architecture: all\n"
            f"Maintainer: {maintainer}\n"
            "Depends: python3 (>= 3.10)\n"
            "Recommends: python3-serial, man-db\n"
            f"Installed-Size: {(size + 1023) // 1024}\n"
            "Homepage: https://github.com/kewl-ua/crsf_ascii_visualizer\n"
            "Description: terminal visualizer for CRSF RC channels\n"
            " Inspect 16 CRSF channels, stick positions and AUX switches over\n"
            " UART, UDP or UNIX sockets. Includes the crsf-viz command and manual.\n",
            encoding="utf-8",
        )
        package = out / f"{PACKAGE}_{release_version}_all.deb"
        command("dpkg-deb", "--root-owner-group", "--build", str(stage), str(package))
        command("dpkg-deb", "--info", str(package))
        # Exercise the packaged launcher independently of the source checkout.
        extracted = Path(directory) / "extract"
        command("dpkg-deb", "--extract", str(package), str(extracted))
        launcher = extracted / "usr" / "bin" / "crsf-viz"
        actual = subprocess.run([str(launcher), "--version"], cwd=directory, check=True, capture_output=True, text=True).stdout.strip()
        if actual != f"crsf-viz {release_version}":
            raise SystemExit(f"Packaged launcher returned an unexpected version: {actual!r}")
        if not (extracted / "usr" / "share" / "man" / "man1" / "crsf-viz.1").is_file():
            raise SystemExit("The Debian package is missing its man page.")
        return package


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-deb", action="store_true")
    parser.add_argument("--maintainer", help="Debian maintainer; defaults to the current public Git commit author")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    release_version = version()
    command(sys.executable, "-m", "build", "--outdir", str(out))
    wheels = list(out.glob(f"crsf_ascii_visualizer-{release_version}-*.whl"))
    sources = list(out.glob(f"crsf_ascii_visualizer-{release_version}.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected one wheel and one source distribution for this version.")
    artifacts = wheels + sources
    if not args.skip_deb:
        artifacts.append(build_deb(out, release_version, args.maintainer))
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in sorted(artifacts))
    (out / "SHA256SUMS").write_text(checksums, encoding="ascii")
    print(checksums, end="")


if __name__ == "__main__":
    main()
