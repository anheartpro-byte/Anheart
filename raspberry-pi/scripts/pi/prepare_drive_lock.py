from __future__ import annotations

import grp
import os
import pwd
import stat
import sys
from pathlib import Path


def prepare() -> None:
    root = os.environ.get("ANHEART_ROOT") or "/"
    if root != "/" and not Path("/.dockerenv").is_file():
        raise RuntimeError("ANHEART_ROOT is only for isolated container tests")
    path = Path(root) / "tmp" / "anheart-drive.lock"
    user = pwd.getpwnam("anheart").pw_uid
    group = grp.getgrnam("dialout").gr_gid
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("drive lock must be a regular file with one link")
        if metadata.st_uid not in {0, user}:
            raise RuntimeError("drive lock belongs to another account; resolve ownership at rest")
        os.fchown(stream.fileno(), user, group)
        os.fchmod(stream.fileno(), 0o660)


def main() -> int:
    if sys.argv[1:] == ["--help"]:
        print("Prepare the shared drive lock without replacing its inode or contents.")
        return 0
    if len(sys.argv) != 1:
        return 2
    try:
        prepare()
    except (OSError, KeyError, RuntimeError) as error:
        print(f"drive lock: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
