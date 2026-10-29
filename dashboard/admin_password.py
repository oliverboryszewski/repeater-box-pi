#!/usr/bin/env python3
"""Set the local dashboard admin password at the Pi console/SSH prompt."""
import argparse
import getpass
import grp
import os
from pathlib import Path
import sys
from controls import password_record, write_json

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="/etc/repeater-box/admin.json")
    args = parser.parse_args()
    try:
        first = getpass.getpass("New dashboard admin password (at least 6 characters): ")
        second = getpass.getpass("Repeat password: ")
        if first != second:
            sys.exit("Passwords do not match. Nothing changed.")
        record = password_record(first)
        del first, second
        path = Path(args.file)
        write_json(path, record, 0o640)
        if os.geteuid() == 0:
            os.chown(path, 0, grp.getgrnam("repeaterdash").gr_gid)
            os.chmod(path, 0o640)
        print("Dashboard admin password saved. Existing sessions will expire automatically.")
    except (OSError, ValueError, KeyError) as error:
        sys.exit(str(error))
