#!/usr/bin/python3
"""Check/apply one narrowly pinned TX name-page recovery experiment.

This local CLI is intentionally absent from the dashboard API. It retains the
pending paired transaction and its immutable candidate. A check opens a read-only
clone session and changes no images, private state or radio bytes. Applying can
restore only the three recorded bytes from that exact protected candidate.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import grp
import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import sys
import uuid

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("name_page_worker", Path(__file__).with_name("program-radio.py"))
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)

OFFSETS = (0x10BD, 0x10BE, 0x10BF)
WANTED = bytes.fromhex("50fafe")
CURRENT = b"\xff" * 3
EXPERIMENT_KEY = "name_page_experiment"
SOURCE_FILES = {"original.img", "upload-base.img", "candidate.img", "observed.img", "verified.img", "manifest.json"}


def require(condition, message):
    if not condition:
        raise w.ProgrammingError(message)


@contextmanager
def maintenance_lock(path, owner_uid=0):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner_uid and not info.st_mode & 0o077,
                "The maintenance lock has invalid ownership or permissions.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def private_bytes(path, owner_uid=0):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == owner_uid
                and not info.st_mode & 0o022 and info.st_size <= 65536,
                "A protected experiment source has invalid ownership or permissions.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(65537)
        require(len(content) <= 65536, "A protected experiment source is too large.")
        return content
    finally:
        os.close(descriptor)


def nonvolatile_differences(first, second):
    require(len(first) == len(second), "The radio image size changed.")
    return {index for index, (old, new) in enumerate(zip(first, second))
            if (old ^ new) & ~w.VOLATILE_MASKS.get(index, 0)}


class Experiment:
    """Fixed TX experiment; tests substitute hardware and private directories."""
    def __init__(self, worker, config):
        self.worker, self.hw, self.config = worker, worker.hardware, config
        self.owner_uid, self.group_id = worker.owner_uid, worker.group_id

    def source(self, pair_id=None, source_id=None):
        require(w.valid_config(self.config), "The local radio configuration is invalid.")
        state = self.worker.private
        pair, tx, rx = (state.get(key) for key in ("_pair", "tx", "rx"))
        require(all(isinstance(item, dict) for item in (pair, tx, rx)), "No complete pending pair record exists.")
        require(pair.get("verification_required") is True and not pair.get("stored_verified")
                and pair.get("phase") == "failed", "This experiment requires the failed pending pair transaction.")
        request_id, program_id = pair.get("request_id"), tx.get("program_id")
        require(isinstance(request_id, str) and w.REQUEST_RE.fullmatch(request_id)
                and isinstance(program_id, str) and w.REQUEST_RE.fullmatch(program_id),
                "The protected pair identifiers are invalid.")
        require((pair_id is None or pair_id == request_id) and (source_id is None or source_id == program_id),
                "The reviewed pair or protected TX candidate changed. Run --check again.")
        require(self.worker.catalog is not None and pair.get("pair_id") in w.pair_plans(self.worker.catalog)
                and pair.get("catalog_digest") == w.catalog_digest(self.worker.catalog), "The pending private pair catalog changed.")
        require(pair.get("devices") == {role: self.config["devices"][role] for role in w.REPEATER_ROLES}
                and tx.get("device") == self.config["devices"]["tx"]
                and rx.get("device") == self.config["devices"]["rx"], "The protected pair's local bindings changed.")
        backup_ids, roles = pair.get("backup_ids", {}), pair.get("roles", {})
        require(isinstance(backup_ids, dict) and backup_ids.get("tx") == program_id
                and backup_ids.get("rx") == rx.get("program_id")
                and tx.get("program_kind") == rx.get("program_kind") == "pair"
                and tx.get("verification_required") is True and rx.get("verified") is True
                and rx.get("verification_required") is False
                and isinstance(roles, dict) and isinstance(roles.get("rx"), dict)
                and roles["rx"].get("written") is True and roles["rx"].get("verified") is True
                and isinstance(roles.get("tx"), dict) and roles["tx"].get("written") is True
                and roles["tx"].get("verified") is False,
                "This is not the recorded RX-verified/TX-pending pair failure.")
        require(tx.get("selected_model", "5RX") == tx.get("program_model", "5RX") == "5RX",
                "This experiment is restricted to the reviewed TX 5RX profile.")
        require(not state.get("programmer", {}).get("verification_required"),
                "Resolve the separate external-programmer upload locally first.")
        require(EXPERIMENT_KEY not in pair, "This pending pair already has an experiment attempt. Its protected backup is retained; do not replay it.")
        for name in (*w.SERVICES, "svxlink.service"):
            require(not self.worker.service("is-active", name), "Radio services must remain paused throughout this experiment.")
        for name in w.SERVICES:
            require(not self.worker.service("is-enabled", name), "Radio autostart must remain disabled throughout this experiment.")
        root = self.worker.backup_dir
        info = root.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner_uid and not info.st_mode & 0o022,
                "The private backup directory has invalid ownership or permissions.")
        folder = root / program_id
        info = folder.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == self.owner_uid and not info.st_mode & 0o022,
                "The protected candidate folder has invalid ownership or permissions.")
        for entry in folder.iterdir():
            require(entry.name in SOURCE_FILES, "The protected candidate folder has unexpected files.")
            private_bytes(entry, self.owner_uid)
        manifest = w.read_json(folder / "manifest.json", private=True, owner_uid=self.owner_uid)
        require(type(manifest.get("schema")) is int and manifest["schema"] == 1 and manifest.get("role") == "tx"
                and manifest.get("device") == self.config["devices"]["tx"]
                and manifest.get("selected_model") == "5RX" and manifest.get("chirp_commit") == w.CHIRP_COMMIT,
                "The protected original metadata does not match this TX connection.")
        original_content = private_bytes(folder / "original.img", self.owner_uid)
        require(hashlib.sha256(original_content).hexdigest() == manifest.get("sha256"),
                "The protected original failed its integrity check.")
        self.hw.select_model("5RX")
        original = self.hw.load(folder / "original.img")
        candidate = self.hw.load(folder / "candidate.img")
        identity = w.image_identity(original)
        require(identity["compatible"] and w.same_identity(manifest, identity)
                and w.same_identity(tx, identity) and w.same_identity(identity, w.image_identity(candidate)),
                "The protected radio identity or firmware does not match.")
        base_path = folder / "upload-base.img"
        if base_path.exists():
            private_bytes(base_path, self.owner_uid)
            base = self.hw.load(base_path)
            require(w.same_identity(identity, w.image_identity(base))
                    and not nonvolatile_differences(w.packed(original), w.packed(base)),
                    "The protected upload base differs from its original backup.")
        else:
            base = self.hw.load(folder / "original.img")
        self.hw.prepare_editor(base, [w.pair_memory(pair["pair_id"], "tx", self.worker.catalog)], {})
        require(not nonvolatile_differences(w.packed(base), w.packed(candidate)),
                "The protected candidate is not the recomputed fixed pair proposal.")
        require(candidate.get_memory(11).empty and original.get_memory(11).empty
                and w.packed(candidate)[OFFSETS[0]:OFFSETS[-1]+1] == WANTED,
                "The protected candidate does not contain the specific recorded unused-name bytes.")
        return pair, tx, candidate, hashlib.sha256(private_bytes(folder / "candidate.img", self.owner_uid)).hexdigest()

    def run(self, apply=False, pair_id=None, source_id=None, output=print):
        require(not apply or isinstance(pair_id, str) and w.REQUEST_RE.fullmatch(pair_id)
                and isinstance(source_id, str) and w.REQUEST_RE.fullmatch(source_id),
                "Applying requires the reviewed --pair-id and --source-id from --check.")
        pair, tx, protected, source_digest = self.source(pair_id, source_id)
        radio = observed = None
        experimental = None
        try:
            radio = self.hw.download(self.config["devices"]["tx"], lambda: None)
            actual, wanted = w.packed(radio), w.packed(protected)
            require(w.same_identity(w.image_identity(radio), w.image_identity(protected)), "The attached TX identity changed.")
            require(nonvolatile_differences(wanted, actual) == set(OFFSETS)
                    and actual[OFFSETS[0]:OFFSETS[-1]+1] == CURRENT and radio.get_memory(11).empty,
                    "The fresh TX read differs from the exact recorded three-byte failure. No repair is authorized.")
            # Recheck root references and the source digest before either the
            # review result or the durable apply preparation can be accepted.
            latest, _tx, _candidate, digest = self.source(pair_id, source_id)
            require(latest["request_id"] == pair["request_id"] and digest == source_digest,
                    "The protected repair source changed during the fresh read.")
            output("Pending pair request: " + pair["request_id"])
            output("Protected TX source: " + tx["program_id"])
            output("Preset: " + pair["pair_id"])
            output("Physical transfer: 0x1000-0x10FF, 16 consecutive 16-byte frames")
            output("Only changed bytes: 0x10BD-0x10BF, FFFFFF -> 50FAFE")
            output("All other bytes, runtime state, radio identity and calibration remain from the fresh TX read.")
            if not apply:
                output("CHECK ONLY: no backup files, private state or radio bytes were written.")
                return {"pair_id": pair["request_id"], "source_id": tx["program_id"], "applied": False}
            identifier = uuid.uuid4().hex
            folder = self.worker.backup(radio, identifier, "original", "tx", self.config["devices"]["tx"])
            original = self.hw.load(folder / "original.img")
            proposal = bytearray(actual)
            proposal[OFFSETS[0]:OFFSETS[-1]+1] = wanted[OFFSETS[0]:OFFSETS[-1]+1]
            require({i for i, (old, new) in enumerate(zip(actual, proposal)) if old != new} == set(OFFSETS),
                    "The experiment proposal changes more than the three recorded bytes.")
            radio.get_mmap().set(0, bytes(proposal)); radio.process_mmap()
            path = folder / "candidate.img"
            radio.save_mmap(str(path)); os.chmod(path, 0o640)
            if self.group_id is not None:
                os.chown(path, 0, self.group_id)
            w.sync_path(path); w.sync_path(folder)
            manifest = w.read_json(folder / "manifest.json", private=True, owner_uid=self.owner_uid)
            experimental = {"schema": 1, "pair_id": pair["request_id"], "source_id": tx["program_id"],
                            "backup_id": identifier, "source_candidate_sha256": source_digest,
                            "original_sha256": manifest["sha256"],
                            "candidate_sha256": hashlib.sha256(private_bytes(path, self.owner_uid)).hexdigest(),
                            "phase": "prepared", "verified": False, "write_started": False,
                            "created_at": datetime.now(timezone.utc).isoformat()}
            pair[EXPERIMENT_KEY] = experimental
            protected_ids = pair.get("protected_backups", [])
            require(isinstance(protected_ids, list) and all(isinstance(token, str) and w.REQUEST_RE.fullmatch(token)
                                                          for token in protected_ids), "The pair backup-protection record is invalid.")
            pair["protected_backups"] = list(dict.fromkeys([*protected_ids, identifier]))
            self.worker.publish()  # Durable pending record before any upload.
            self.worker.prune_backups()  # One attempt per pair, five sets per role.

            def before_write():
                experimental.update(phase="writing", write_started=True)
                self.worker.publish()

            output("Experimental backup saved and protected. Starting the reviewed contiguous name transfer.")
            blocks = w.changed_blocks(original, actual, bytes(proposal))
            self.hw.upload(self.config["devices"]["tx"], original, radio, blocks, lambda: None,
                           before_write, editor=True)
            experimental["phase"] = "verifying"
            self.worker.publish()
            observed = self.hw.settle_and_verify(self.config["devices"]["tx"], radio, lambda *args: None)
            self.worker.save_observed(observed, folder)
            w.verify_images(original, radio, observed)
            experimental.update(phase="verified", verified=True)
            self.worker.publish()
            output("Experimental transfer passed full-image verification. The paired transaction remains pending and radio services remain paused.")
            return {"pair_id": pair["request_id"], "source_id": tx["program_id"],
                    "backup_id": identifier, "applied": True, "verified": True}
        except Exception:
            if experimental is not None:
                experimental.update(phase="failed", verified=False)
                try:
                    self.worker.publish()
                except Exception:
                    pass  # Existing paired interlocks remain durably pending.
            raise
        finally:
            self.hw.release(observed)
            self.hw.release(radio)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read and review only (default)")
    mode.add_argument("--apply", action="store_true", help="Apply the exact reviewed one-attempt experiment")
    parser.add_argument("--pair-id", help="Pending pair request token printed by --check")
    parser.add_argument("--source-id", help="Protected TX source token printed by --check")
    args = parser.parse_args(argv)
    if not args.apply and (args.pair_id is not None or args.source_id is not None):
        parser.error("--pair-id and --source-id are used only with --apply")
    if os.geteuid() != 0:
        parser.error("Run this local experiment with sudo")
    try:
        with maintenance_lock(w.LOCK_PATH):
            config = w.read_json(w.CONFIG_PATH, 4096, private=True)
            group_id = grp.getgrnam("repeaterdash").gr_gid
            hw = w.Hardware()
            worker = w.Worker(hw, group_id=group_id)
            Experiment(worker, config).run(args.apply, args.pair_id, args.source_id)
        return 0
    except w.ProgrammingError as exc:
        print("Experiment stopped: " + str(exc), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("Experiment stopped: a private source, lock or radio connection could not be validated. The pair remains pending.", file=sys.stderr)
    except Exception:
        print("Experiment stopped: the radio did not complete the operation. The pair remains pending; review its protected backups locally.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
