"""Password hashing and verification for the web session."""

from __future__ import annotations

import getpass

from passlib.hash import argon2


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password against an Argon2 hash."""
    if not password_hash:
        return False
    try:
        return argon2.verify(password, password_hash)
    except (TypeError, ValueError):
        return False


def hash_password(password: str) -> str:
    """Hash a password with Argon2."""
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters long")
    return argon2.hash(password)


def main() -> None:
    """Read a password interactively and print its Argon2 hash."""
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm:  ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    try:
        print(hash_password(password))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
