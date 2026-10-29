#!/usr/bin/python3
"""Configure station-specific channel pairs locally; never program a radio."""
import argparse
import ast
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import grp
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

SPEC = importlib.util.spec_from_file_location("private_channel_schema", Path(__file__).with_name("radio_plan.py"))
schema = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schema)
CONFIG_PATH = Path("/etc/repeater-box/channel-pairs.json")
WORKER_PATH = Path("/opt/repeater-box/scripts/program-radio.py")
STATE_PATH = Path("/var/lib/repeater-radio-backups/state.json")
LOCK_PATH = Path("/opt/repeater-box/.maintenance.lock")


def private_content(path, owner_uid=0, maximum=262144, confidential=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                or info.st_mode & 0o022 or confidential and info.st_mode & 0o007
                or info.st_size > maximum):
            raise ValueError("A local configuration source has invalid ownership, permissions or size.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("A local configuration source is too large.")
        return content
    finally:
        os.close(descriptor)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate local configuration key.")
        value[key] = item
    return value


def private_json(path, owner_uid=0):
    value = json.loads(private_content(path, owner_uid, confidential=True), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("The local configuration must be a JSON object.")
    return value


def require_no_pending(state_path=STATE_PATH, owner_uid=0):
    try:
        state = private_json(state_path, owner_uid)
    except FileNotFoundError:
        return
    if any(isinstance(item, dict) and item.get("verification_required") for item in state.values()):
        raise ValueError("Resolve the unfinished radio upload or pair transaction before changing channel-pair configuration.")


def checked_directory(path, owner_uid=0):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
        raise ValueError("The local configuration directory must be owned by root and not writable by other users.")


def save_catalog(value, config_path=CONFIG_PATH, state_path=STATE_PATH, owner_uid=0, group_id=None):
    """The caller holds the maintenance lock. Never touch radio or backup files."""
    canonical = schema.validate_catalog(value)
    require_no_pending(state_path, owner_uid)
    checked_directory(config_path.parent, owner_uid)
    try:
        previous = schema.validate_catalog(private_json(config_path, owner_uid))
    except FileNotFoundError:
        previous = None
    if previous == canonical:
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=".channel-pairs-", dir=config_path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        if group_id is not None:
            os.fchown(descriptor, owner_uid, group_id)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(canonical, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        directory = os.open(config_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def parse_mhz(value):
    try:
        number = Decimal(value.strip()) * 1000000
        if not number.is_finite() or number != number.to_integral_value() or int(number) % 10:
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError("Enter a frequency in MHz with 10 Hz resolution.") from None


def legacy_catalog(content):
    """Read literal old tables with AST; never import or execute the old helper."""
    tree = ast.parse(content)
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("PLAN", "PAIR_PLANS"):
                    if target.id in values:
                        raise ValueError("The installed helper has ambiguous legacy channel data.")
                    values[target.id] = ast.literal_eval(node.value)
    if not values.get("PLAN") and not values.get("PAIR_PLANS"):
        return None
    plan, pairs = values.get("PLAN"), values.get("PAIR_PLANS")
    if (not isinstance(plan, dict) or set(plan) != {"rx", "tx"}
            or not isinstance(pairs, dict) or not 1 <= len(pairs) <= 9):
        raise ValueError("The installed legacy pair configuration needs local review before migration.")
    try:
        rx = parse_mhz(plan["rx"]["frequency_mhz"])
        tx = parse_mhz(plan["tx"]["frequency_mhz"])
        tone = plan["rx"]["receive_tone_hz"]
        if tone != plan["tx"]["transmit_tone_hz"]:
            raise ValueError("The legacy RX/TX tones differ; configure the private catalog locally.")
        matches = [identifier for identifier, item in pairs.items()
                   if isinstance(item, dict) and item.get("rx_hz") == rx and item.get("tx_hz") == tx]
        if not matches:
            raise ValueError("The legacy default pair does not match its catalog.")
        default_id = "default" if "default" in matches else matches[0]
        identifiers = [default_id, *(identifier for identifier in pairs if identifier != default_id)]
        result = {"schema": 1, "default_pair_id": default_id, "pairs": [
            {"id": identifier, "label": f"Pair {index}", "rx_hz": pairs[identifier]["rx_hz"],
             "tx_hz": pairs[identifier]["tx_hz"], "tone_hz": tone}
            for index, identifier in enumerate(identifiers, 1)]}
    except (KeyError, TypeError):
        raise ValueError("The installed legacy pair configuration is incomplete.") from None
    return schema.validate_catalog(result)


def migrate_installed(config_path=CONFIG_PATH, worker_path=WORKER_PATH, state_path=STATE_PATH,
                      owner_uid=0, group_id=None, output=print):
    """Called under the installer's existing lock, before replacing its helper."""
    checked_directory(config_path.parent, owner_uid)
    try:
        catalog = schema.validate_catalog(private_json(config_path, owner_uid))
    except FileNotFoundError:
        catalog = None
    if catalog is not None:
        try:
            state = private_json(state_path, owner_uid)
        except FileNotFoundError:
            state = {}
        held = state.get("_pair", {})
        if (isinstance(held, dict) and held.get("verification_required")
                and held.get("catalog_digest") != schema.catalog_digest(catalog)):
            raise ValueError("Resolve the pending pair with its installed helper before migrating to the private catalog.")
        output(f"Preserved {len(catalog['pairs'])} private channel pairs.")
        return True
    try:
        content = private_content(worker_path, owner_uid)
    except FileNotFoundError:
        output("No channel pairs configured. Configure them locally after installation.")
        return False
    require_no_pending(state_path, owner_uid)
    catalog = legacy_catalog(content)
    if catalog is None:
        output("No channel pairs configured. Configure them locally after installation.")
        return False
    save_catalog(catalog, config_path, state_path, owner_uid, group_id)
    output(f"Migrated {len(catalog['pairs'])} channel pairs into private Pi configuration. No radio was programmed.")
    return True


@contextmanager
def maintenance_lock(path=LOCK_PATH):
    checked_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError("The maintenance lock has invalid ownership or permissions.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def interactive(input_fn=input, output=print):
    output("Configure your own locally suitable VHF input / UHF output pairs. Nothing is uploaded to GitHub.")
    output("Pair 1 is Default. Radio names are BOX-RX1 / BOX-TX1 through BOX-RX9 / BOX-TX9.")
    count = int(input_fn("How many pairs (1-9)? ").strip())
    if not 1 <= count <= 9:
        raise ValueError("Choose one to nine channel pairs.")
    pairs = []
    for index in range(1, count + 1):
        label = input_fn(f"Pair {index} label (blank for Pair {index}): ").strip() or f"Pair {index}"
        rx = parse_mhz(input_fn(f"Pair {index} RX/input MHz: "))
        tx = parse_mhz(input_fn(f"Pair {index} TX/output MHz: "))
        entered = input_fn(f"Pair {index} CTCSS Hz (blank for none): ").strip()
        tone = float(entered) if entered else None
        pairs.append({"id": f"pair{index}", "label": label, "rx_hz": rx, "tx_hz": tx, "tone_hz": tone})
    catalog = schema.validate_catalog({"schema": 1, "default_pair_id": "pair1", "pairs": pairs})
    for identifier, plan in schema.pair_plans(catalog).items():
        tone = "Off" if plan["tone_hz"] is None else f"{plan['tone_hz']:g} Hz"
        output(f"{plan['label']}: {plan['rx_name']} {plan['rx_hz']/1000000:.6f} MHz -> "
               f"{plan['tx_name']} {plan['tx_hz']/1000000:.6f} MHz; CTCSS {tone}")
    if input_fn("Save these pairs on this Pi (yes/no)? ").strip().lower() != "yes":
        output("Cancelled. The private configuration was not changed.")
        return None
    return catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-installed", action="store_true", help="Preserve an existing private catalog or migrate the installed legacy helper")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Run locally with sudo")
    try:
        group_id = grp.getgrnam("repeaterdash").gr_gid
        with maintenance_lock():
            if args.migrate_installed:
                migrate_installed(group_id=group_id)
            else:
                require_no_pending()
                catalog = interactive()
                if catalog is not None:
                    save_catalog(catalog, group_id=group_id)
                    print("Private channel pairs saved. Refresh the dashboard and review a paired write to apply them to the radios.")
        return 0
    except (OSError, ValueError, KeyError, SyntaxError) as error:
        print("Channel-pair configuration stopped: " + str(error), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled. No radio was programmed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
