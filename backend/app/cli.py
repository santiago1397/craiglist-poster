"""Small utilities usable outside the running server."""
from __future__ import annotations

import getpass
import sys

from .auth import hash_password


def hash_password_cmd() -> int:
    """Prompt for a password twice, print the argon2id hash. Paste into .env.prod."""
    pw1 = getpass.getpass("New admin password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(pw1) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 1
    print(hash_password(pw1))
    return 0


# Entry-point wrapper
def hash_password() -> None:  # type: ignore[no-redef]
    raise SystemExit(hash_password_cmd())
