"""Interactively generate a Werkzeug hash without echoing or logging the password."""

from getpass import getpass

from werkzeug.security import generate_password_hash


def main() -> int:
    password = getpass("Web login password: ")
    confirmation = getpass("Confirm password: ")
    if not password:
        raise SystemExit("Password must not be empty")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    print(generate_password_hash(password, method="scrypt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
