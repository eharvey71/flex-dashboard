"""Secret storage backed by the macOS login Keychain.

Nothing sensitive is ever written to disk in this project. Each secret lives
under service name "flex-dashboard" with the account name used as the key.
"""

import subprocess
import sys

SERVICE = "flex-dashboard"


class MissingSecret(Exception):
    pass


def get(key, required=True):
    """Read one secret from the Keychain. Returns None if absent and optional."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", key, "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except FileNotFoundError:
        raise MissingSecret("The `security` command is missing. This must run on macOS, not in a Linux VM.")
    except subprocess.CalledProcessError:
        if required:
            raise MissingSecret(
                "No Keychain entry for '%s'.\n"
                "  Add it with:\n"
                "    security add-generic-password -s %s -a %s -T /usr/bin/python3 -U -w"
                % (key, SERVICE, key)
            )
        return None


def put(key, value):
    """Write or replace one secret. -T lets python3 read it without prompting."""
    subprocess.run(
        ["security", "add-generic-password", "-s", SERVICE, "-a", key,
         "-T", "/usr/bin/python3", "-U", "-w", value],
        check=True,
    )


if __name__ == "__main__":
    # Interactive helper: python3 creds.py set <key>
    if len(sys.argv) == 3 and sys.argv[1] == "set":
        import getpass
        put(sys.argv[2], getpass.getpass("Value for %s: " % sys.argv[2]))
        print("Stored %s." % sys.argv[2])
    else:
        print("usage: python3 creds.py set <key>")
