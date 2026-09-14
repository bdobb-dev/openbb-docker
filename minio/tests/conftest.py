import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "cert-sync.sh"


@pytest.fixture
def fake_tailscale(tmp_path):
    """A stub `tailscale` on PATH whose emitted cert body is controllable.

    Writing to <bindir>/cert-body changes what the next invocation emits.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = bindir / "cert-body"
    body.write_text("CERT-A")
    stub = bindir / "tailscale"
    stub.write_text(
        "#!/bin/sh\n"
        "# usage: tailscale cert --cert-file X --key-file Y DOMAIN\n"
        'if [ -f "$(dirname "$0")/fail" ]; then echo "boom" >&2; exit 1; fi\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    --cert-file) shift; CERT="$1" ;;\n'
        '    --key-file) shift; KEY="$1" ;;\n'
        "  esac\n"
        "  shift\n"
        "done\n"
        'cat "$(dirname "$0")/cert-body" > "$CERT"\n'
        'echo "KEY" > "$KEY"\n'
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bindir


@pytest.fixture
def run_sync(fake_tailscale):
    def _run(cert_dir, domain, pid_file):
        env = dict(os.environ, PATH=f"{fake_tailscale}:{os.environ['PATH']}")
        return subprocess.run(
            ["sh", str(SCRIPT), str(cert_dir), domain, str(pid_file)],
            capture_output=True, text=True, env=env,
        )
    return _run
