#!/usr/bin/env python3
"""SQLite inventory and collision-safe name allocation for standalone UTM clones."""

from __future__ import annotations

import re
import secrets
import sqlite3
import string
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


NAME_RE = re.compile(r"^[a-z]{4}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
RESERVED_NAMES = {"macos"}


class InventoryError(RuntimeError):
    """Raised when the inventory cannot safely reserve or update a VM."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise InventoryError("inventory database must not be a symlink")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def ensure_schema(path: Path) -> None:
    connection = _connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS vm_inventory (
                vm_name TEXT PRIMARY KEY CHECK (length(vm_name) = 4 AND vm_name GLOB '[a-z][a-z][a-z][a-z]'),
                config_uuid TEXT UNIQUE,
                bundle_path TEXT,
                mac_address TEXT,
                machine_identifier_sha256 TEXT,
                status TEXT NOT NULL CHECK (status IN ('reserved', 'copying', 'complete', 'failed')),
                attempt_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reserved_at TEXT,
                vm_created_at TEXT,
                last_used_at TEXT,
                application_name TEXT,
                reusable INTEGER NOT NULL DEFAULT 0 CHECK (reusable IN (0, 1)),
                available INTEGER NOT NULL DEFAULT 0 CHECK (available IN (0, 1)),
                directory_present INTEGER NOT NULL DEFAULT 1 CHECK (directory_present IN (0, 1)),
                last_scan_at TEXT,
                guest_serial_number TEXT,
                guest_platform_uuid TEXT,
                guest_mac_address TEXT,
                guest_identity_sha256 TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vm_inventory_status ON vm_inventory(status);
            CREATE INDEX IF NOT EXISTS idx_vm_inventory_reusable ON vm_inventory(vm_name, reusable);
            CREATE INDEX IF NOT EXISTS idx_vm_inventory_application ON vm_inventory(application_name);
            """
        )
        existing_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(vm_inventory)")
        }
        migrations = {
            "reserved_at": "TEXT",
            "vm_created_at": "TEXT",
            "last_used_at": "TEXT",
            "application_name": "TEXT",
            "reusable": "INTEGER NOT NULL DEFAULT 0",
            "available": "INTEGER NOT NULL DEFAULT 0",
            "directory_present": "INTEGER NOT NULL DEFAULT 1",
            "last_scan_at": "TEXT",
            "guest_serial_number": "TEXT",
            "guest_platform_uuid": "TEXT",
            "guest_mac_address": "TEXT",
            "guest_identity_sha256": "TEXT",
        }
        for column, declaration in migrations.items():
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE vm_inventory ADD COLUMN {column} {declaration}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_inventory_guest_serial ON vm_inventory(guest_serial_number)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_inventory_guest_uuid ON vm_inventory(guest_platform_uuid)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_inventory_guest_mac ON vm_inventory(guest_mac_address)"
        )
        # Older databases only had created_at. Preserve that historical time
        # as the first known VM creation time after adding the explicit field.
        connection.execute(
            "UPDATE vm_inventory SET vm_created_at=created_at WHERE vm_created_at IS NULL"
        )
        connection.commit()
    finally:
        connection.close()
    try:
        path.chmod(0o600)
    except OSError as error:
        raise InventoryError(f"cannot set inventory mode 600: {error}") from error


def _used_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT vm_name FROM vm_inventory WHERE COALESCE(reusable, 0)=0"
        )
    }


def reserve_vm_name(
    database: Path,
    images_dir: Path,
    *,
    candidate_factory: Callable[[], str] | None = None,
    max_attempts: int = 512,
) -> str:
    """Atomically reserve a unique four-letter name across DB and bundles."""
    ensure_schema(database)
    images_dir = Path(images_dir).expanduser()
    if images_dir.is_symlink():
        raise InventoryError("images directory must not be a symlink")
    images_dir = images_dir.resolve()
    images_dir.mkdir(parents=True, exist_ok=True)
    existing = {path.stem for path in images_dir.glob("*.utm") if path.is_dir() or path.is_file()}
    factory = candidate_factory or (lambda: "".join(secrets.choice(string.ascii_lowercase) for _ in range(4)))
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        used = _used_names(connection) | existing | RESERVED_NAMES
        for _ in range(max_attempts):
            candidate = str(factory()).strip().lower()
            if not NAME_RE.fullmatch(candidate) or candidate in used:
                continue
            now = _now()
            try:
                existing_row = connection.execute(
                    "SELECT vm_name FROM vm_inventory WHERE vm_name=?", (candidate,)
                ).fetchone()
                if existing_row is None:
                    connection.execute(
                        """INSERT INTO vm_inventory(
                           vm_name, status, created_at, updated_at, reserved_at,
                           reusable, available, directory_present
                        ) VALUES (?, 'reserved', ?, ?, ?, 0, 0, 0)""",
                        (candidate, now, now, now),
                    )
                else:
                    cursor = connection.execute(
                        """UPDATE vm_inventory SET status='reserved', updated_at=?, reserved_at=?,
                           bundle_path=NULL, config_uuid=NULL, mac_address=NULL,
                           machine_identifier_sha256=NULL, vm_created_at=NULL,
                           application_name=NULL, last_used_at=NULL,
                           reusable=0, available=0, directory_present=0, last_scan_at=NULL
                           WHERE vm_name=? AND COALESCE(reusable, 0)=1""",
                        (now, now, candidate),
                    )
                    if cursor.rowcount != 1:
                        used.add(candidate)
                        continue
                connection.commit()
                return candidate
            except sqlite3.IntegrityError:
                used.add(candidate)
                continue
        connection.rollback()
        raise InventoryError("unique VM name allocation exhausted")
    finally:
        connection.close()


def update_status(database: Path, vm_name: str, status: str, *, attempt_id: str | None = None) -> None:
    if not NAME_RE.fullmatch(vm_name) or status not in {"reserved", "copying", "complete", "failed"}:
        raise InventoryError("invalid VM inventory update")
    ensure_schema(database)
    connection = _connect(database)
    try:
        values = [status, _now(), vm_name]
        query = "UPDATE vm_inventory SET status=?, updated_at=?"
        if attempt_id is not None:
            query += ", attempt_id=?"
            values.insert(2, attempt_id)
        query += " WHERE vm_name=?"
        cursor = connection.execute(query, values)
        if cursor.rowcount != 1:
            raise InventoryError(f"VM name is not reserved: {vm_name}")
        connection.commit()
    finally:
        connection.close()


def register_clone(
    database: Path,
    vm_name: str,
    config_uuid: str,
    bundle_path: Path,
    mac_address: str,
    machine_identifier_sha256: str,
    *,
    attempt_id: str | None = None,
    status: str = "complete",
) -> None:
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    if not UUID_RE.fullmatch(config_uuid):
        raise InventoryError("invalid config UUID")
    if not MAC_RE.fullmatch(mac_address.lower()):
        raise InventoryError("invalid MAC address")
    if status not in {"reserved", "copying", "complete", "failed"}:
        raise InventoryError("invalid VM status")
    ensure_schema(database)
    connection = _connect(database)
    try:
        now = _now()
        cursor = connection.execute(
            """UPDATE vm_inventory
               SET config_uuid=?, bundle_path=?, mac_address=?, machine_identifier_sha256=?,
                   status=?, attempt_id=COALESCE(?, attempt_id), updated_at=?, vm_created_at=?,
                   application_name=NULL, last_used_at=NULL,
                   reusable=0, available=0, directory_present=0, last_scan_at=NULL
               WHERE vm_name=?""",
            (
                config_uuid.upper(),
                str(Path(bundle_path).expanduser().resolve()),
                mac_address.lower(),
                machine_identifier_sha256,
                status,
                attempt_id,
                now,
                now,
                vm_name,
            ),
        )
        if cursor.rowcount != 1:
            raise InventoryError(f"VM name is not reserved: {vm_name}")
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise InventoryError(f"clone identity conflicts with inventory: {error}") from error
    finally:
        connection.close()


def _validate_application_name(application_name: str) -> str:
    value = str(application_name).strip()
    if not value:
        raise InventoryError("application name must not be empty")
    return value


def _assert_application_available(
    connection: sqlite3.Connection,
    application_name: str,
    vm_name: str,
) -> None:
    # Test fixtures are intentionally allowed to share the literal name
    # "test". Every other application name is one-to-one with a VM record.
    if application_name == "test":
        return
    row = connection.execute(
        "SELECT vm_name FROM vm_inventory WHERE application_name=? AND vm_name<>? LIMIT 1",
        (application_name, vm_name),
    ).fetchone()
    if row is not None:
        raise InventoryError(
            f"application name is already assigned to VM {row['vm_name']}: {application_name}"
        )


def register_existing(
    database: Path,
    vm_name: str,
    config_uuid: str,
    bundle_path: Path,
    mac_address: str,
    machine_identifier_sha256: str,
    application_name: str,
    *,
    vm_created_at: str | None = None,
    available: bool = False,
) -> None:
    """Register a manually confirmed existing bundle without starting it.

    This is intentionally separate from automatic reconciliation. It is
    idempotent for the same name/identity and permits reuse of a row previously
    marked reusable, but it never assumes initialization is complete: callers
    must explicitly pass ``available=True`` only after independent evidence.
    """
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    if not UUID_RE.fullmatch(config_uuid):
        raise InventoryError("invalid config UUID")
    if not MAC_RE.fullmatch(mac_address.lower()):
        raise InventoryError("invalid MAC address")
    application_name = _validate_application_name(application_name)
    bundle = Path(bundle_path).expanduser()
    if bundle.is_symlink() or not bundle.is_dir():
        raise InventoryError("existing VM bundle must be a non-symlink directory")
    bundle = bundle.resolve()
    ensure_schema(database)
    connection = _connect(database)
    now = _now()
    created = vm_created_at or now
    try:
        connection.execute("BEGIN IMMEDIATE")
        _assert_application_available(connection, application_name, vm_name)
        row = connection.execute(
            "SELECT * FROM vm_inventory WHERE vm_name=?", (vm_name,)
        ).fetchone()
        identity = (
            config_uuid.upper(),
            str(bundle),
            mac_address.lower(),
            machine_identifier_sha256,
        )
        if row is None:
            connection.execute(
                """INSERT INTO vm_inventory(
                   vm_name, config_uuid, bundle_path, mac_address,
                   machine_identifier_sha256, status, created_at, updated_at,
                   reserved_at, vm_created_at, application_name, reusable,
                   available, directory_present, last_scan_at
                ) VALUES (?, ?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?, 0, ?, 1, ?)""",
                (
                    vm_name,
                    *identity,
                    now,
                    now,
                    now,
                    created,
                    application_name,
                    1 if available else 0,
                    now,
                ),
            )
        else:
            if (
                not bool(row["reusable"])
                and row["application_name"] not in (None, application_name)
            ):
                raise InventoryError(
                    f"application name conflict for registered VM: {vm_name}"
                )
            current_identity = (
                str(row["config_uuid"] or "").upper(),
                str(row["bundle_path"] or ""),
                str(row["mac_address"] or "").lower(),
                str(row["machine_identifier_sha256"] or ""),
            )
            if not bool(row["reusable"]) and current_identity != identity:
                raise InventoryError(
                    f"registered VM identity conflict for non-reusable name: {vm_name}"
                )
            connection.execute(
                """UPDATE vm_inventory SET config_uuid=?, bundle_path=?, mac_address=?,
                   machine_identifier_sha256=?, status='complete', updated_at=?,
                   vm_created_at=?, application_name=?, reusable=0, available=?,
                   directory_present=1, last_scan_at=? WHERE vm_name=?""",
                (
                    *identity,
                    now,
                    created,
                    application_name,
                    1 if available else 0,
                    now,
                    vm_name,
                ),
            )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise InventoryError(f"existing VM registration conflicts with inventory: {error}") from error
    finally:
        connection.close()


def get_record(database: Path, vm_name: str) -> dict[str, object]:
    ensure_schema(database)
    connection = _connect(database)
    try:
        row = connection.execute("SELECT * FROM vm_inventory WHERE vm_name=?", (vm_name,)).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        return dict(row)
    finally:
        connection.close()


def list_records(database: Path) -> list[dict[str, object]]:
    """Return every historical reservation/clone for identity collision checks."""
    ensure_schema(database)
    connection = _connect(database)
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM vm_inventory ORDER BY vm_name")]
    finally:
        connection.close()


def record_guest_identity(
    database: Path,
    vm_name: str,
    serial_number: str,
    platform_uuid: str,
    guest_mac_address: str,
) -> None:
    """Persist and de-duplicate the guest's three identity values."""
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    serial = str(serial_number).strip()
    if not serial or any(ord(char) < 32 for char in serial):
        raise InventoryError("invalid guest serial number")
    if not UUID_RE.fullmatch(str(platform_uuid).strip()):
        raise InventoryError("invalid guest platform UUID")
    guest_uuid = str(platform_uuid).strip().upper()
    guest_mac = normalize_guest_mac(guest_mac_address)
    identity_digest = hashlib.sha256(
        f"{serial}\n{guest_uuid}\n{guest_mac}".encode("utf-8")
    ).hexdigest()
    ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM vm_inventory WHERE vm_name=?", (vm_name,)
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        for column, value in (
            ("guest_serial_number", serial),
            ("guest_platform_uuid", guest_uuid),
            ("guest_mac_address", guest_mac),
        ):
            conflict = connection.execute(
                f"SELECT vm_name FROM vm_inventory WHERE {column}=? AND vm_name<>? LIMIT 1",
                (value, vm_name),
            ).fetchone()
            if conflict is not None:
                raise InventoryError(
                    f"guest identity {column} is already registered to VM {conflict['vm_name']}"
                )
        current = (
            row["guest_serial_number"],
            row["guest_platform_uuid"],
            row["guest_mac_address"],
        )
        desired = (serial, guest_uuid, guest_mac)
        if any(value is not None for value in current) and tuple(current) != desired:
            raise InventoryError("guest identity differs from the existing VM record")
        connection.execute(
            """UPDATE vm_inventory SET guest_serial_number=?, guest_platform_uuid=?,
               guest_mac_address=?, guest_identity_sha256=?, updated_at=? WHERE vm_name=?""",
            (serial, guest_uuid, guest_mac, identity_digest, _now(), vm_name),
        )
        connection.commit()
    finally:
        connection.close()


def update_guest_mac(database: Path, vm_name: str, guest_mac_address: str) -> None:
    """Update only the guest MAC after an approved UTM-1 network change.

    Serial number and platform UUID are immutable guest identity values.  The
    MAC is intentionally mutable because UTM-1 randomizes the saved network
    address while the VM is stopped; the next boot exposes that new address
    inside macOS.
    """
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    guest_mac = normalize_guest_mac(guest_mac_address)
    ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT guest_serial_number, guest_platform_uuid FROM vm_inventory WHERE vm_name=?",
            (vm_name,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        if not row["guest_serial_number"] or not row["guest_platform_uuid"]:
            raise InventoryError("guest serial number and platform UUID must be recorded first")
        conflict = connection.execute(
            """SELECT vm_name FROM vm_inventory
               WHERE (guest_mac_address=? OR mac_address=?) AND vm_name<>? LIMIT 1""",
            (guest_mac, guest_mac, vm_name),
        ).fetchone()
        if conflict is not None:
            raise InventoryError(
                f"guest identity guest_mac_address is already registered to VM {conflict['vm_name']}"
            )
        digest = hashlib.sha256(
            f"{row['guest_serial_number']}\n{row['guest_platform_uuid']}\n{guest_mac}".encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE vm_inventory SET mac_address=?, guest_mac_address=?, guest_identity_sha256=?,
               updated_at=? WHERE vm_name=?""",
            (guest_mac, guest_mac, digest, _now(), vm_name),
        )
        connection.commit()
    finally:
        connection.close()


def normalize_guest_mac(value: str) -> str:
    compact = re.sub(r"[:-]", "", str(value).strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", compact):
        raise InventoryError("invalid guest MAC address")
    normalized = ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    first = int(normalized[:2], 16)
    if not (first & 0x02) or (first & 0x01):
        raise InventoryError("guest MAC must be locally administered unicast")
    return normalized


def mark_used(
    database: Path,
    vm_name: str,
    application_name: str,
    *,
    used_at: str | None = None,
) -> None:
    """Bind one VM to one application and remove it from the available pool."""
    ensure_schema(database)
    timestamp = used_at or _now()
    application_name = _validate_application_name(application_name)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT application_name FROM vm_inventory WHERE vm_name=?",
            (vm_name,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        current_application = row["application_name"]
        if current_application not in (None, application_name):
            raise InventoryError(
                f"VM is already assigned to application {current_application}: {vm_name}"
            )
        _assert_application_available(connection, application_name, vm_name)
        cursor = connection.execute(
            """UPDATE vm_inventory SET application_name=?, last_used_at=?,
               available=0, updated_at=? WHERE vm_name=?""",
            (application_name, timestamp, _now(), vm_name),
        )
        if cursor.rowcount != 1:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        connection.commit()
    finally:
        connection.close()


def rebind_application_name(
    database: Path,
    vm_name: str,
    old_application_name: str,
    new_application_name: str,
) -> None:
    """Rename one exact VM binding only after an explicit old-name match."""
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    old_name = _validate_application_name(old_application_name)
    new_name = _validate_application_name(new_application_name)
    if old_name == new_name:
        raise InventoryError("old and new application names must differ")
    ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT application_name, status, directory_present, reusable
               FROM vm_inventory WHERE vm_name=?""",
            (vm_name,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        if (
            row["status"] != "complete"
            or int(row["directory_present"] or 0) != 1
            or int(row["reusable"] or 0) != 0
        ):
            raise InventoryError(f"VM is not complete and present: {vm_name}")
        current = row["application_name"]
        if current == new_name:
            connection.commit()
            return
        if current != old_name:
            raise InventoryError(
                f"old application binding does not match {vm_name}: {current}"
            )
        _assert_application_available(connection, new_name, vm_name)
        cursor = connection.execute(
            """UPDATE vm_inventory SET application_name=?, available=0,
               updated_at=? WHERE vm_name=? AND application_name=?""",
            (new_name, _now(), vm_name, old_name),
        )
        if cursor.rowcount != 1:
            raise InventoryError("application rename lost its atomic guard")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_available_vm(
    database: Path,
    application_name: str,
    *,
    used_at: str | None = None,
) -> str:
    """Atomically bind one initialized pool VM to an application.

    A retry for the same application returns its existing binding. A new claim
    can only consume a complete, present, unbound row whose availability flag
    is set.
    """
    ensure_schema(database)
    application_name = _validate_application_name(application_name)
    timestamp = used_at or _now()
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT vm_name, status, directory_present, reusable
               FROM vm_inventory WHERE application_name=? ORDER BY vm_name""",
            (application_name,),
        ).fetchall()
        if len(existing) > 1:
            raise InventoryError(
                f"application name has multiple VM bindings: {application_name}"
            )
        if existing:
            row = existing[0]
            if (
                row["status"] != "complete"
                or int(row["directory_present"] or 0) != 1
                or int(row["reusable"] or 0) != 0
            ):
                raise InventoryError(
                    f"existing application VM is not usable: {row['vm_name']}"
                )
            connection.execute(
                "UPDATE vm_inventory SET available=0, updated_at=? WHERE vm_name=?",
                (_now(), row["vm_name"]),
            )
            connection.commit()
            return str(row["vm_name"])

        row = connection.execute(
            """SELECT vm_name FROM vm_inventory
               WHERE available=1 AND application_name IS NULL
                 AND status='complete' AND directory_present=1 AND reusable=0
               ORDER BY vm_name LIMIT 1"""
        ).fetchone()
        if row is None:
            raise InventoryError("no initialized unbound VM is available")
        vm_name = str(row["vm_name"])
        cursor = connection.execute(
            """UPDATE vm_inventory SET application_name=?, available=0,
               last_used_at=?, updated_at=?
               WHERE vm_name=? AND available=1 AND application_name IS NULL
                 AND status='complete' AND directory_present=1 AND reusable=0""",
            (application_name, timestamp, _now(), vm_name),
        )
        if cursor.rowcount != 1:
            raise InventoryError("available VM claim lost its atomic guard")
        connection.commit()
        return vm_name
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_exact_available_vm(
    database: Path,
    vm_name: str,
    application_name: str,
    *,
    used_at: str | None = None,
) -> str:
    """Atomically bind one explicitly named available VM.

    Unlike :func:`claim_available_vm`, this function never selects the first
    pool member.  It is used only when the caller already has an exact VM name
    and must prove that same row is complete, present, and claimable.
    """
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    ensure_schema(database)
    application_name = _validate_application_name(application_name)
    timestamp = used_at or _now()
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT application_name, available, status, directory_present, reusable
               FROM vm_inventory WHERE vm_name=?""",
            (vm_name,),
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        if (
            row["status"] != "complete"
            or int(row["directory_present"] or 0) != 1
            or int(row["reusable"] or 0) != 0
        ):
            raise InventoryError(f"exact VM is not complete and present: {vm_name}")
        current_application = row["application_name"]
        if current_application == application_name:
            connection.execute(
                "UPDATE vm_inventory SET available=0, updated_at=? WHERE vm_name=?",
                (_now(), vm_name),
            )
            connection.commit()
            return vm_name
        if current_application is not None:
            raise InventoryError(
                f"VM is already assigned to application {current_application}: {vm_name}"
            )
        if int(row["available"] or 0) != 1:
            raise InventoryError(f"exact VM is not available: {vm_name}")
        _assert_application_available(connection, application_name, vm_name)
        cursor = connection.execute(
            """UPDATE vm_inventory SET application_name=?, available=0,
               last_used_at=?, updated_at=?
               WHERE vm_name=? AND application_name IS NULL AND available=1
                 AND status='complete' AND directory_present=1 AND reusable=0""",
            (application_name, timestamp, _now(), vm_name),
        )
        if cursor.rowcount != 1:
            raise InventoryError("exact VM claim lost its atomic guard")
        connection.commit()
        return vm_name
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_available(database: Path, vm_name: str, available: bool) -> None:
    """Set the clone+initialization availability flag after independent verification."""
    ensure_schema(database)
    connection = _connect(database)
    try:
        cursor = connection.execute(
            "UPDATE vm_inventory SET available=?, updated_at=? WHERE vm_name=?",
            (1 if available else 0, _now(), vm_name),
        )
        if cursor.rowcount != 1:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        connection.commit()
    finally:
        connection.close()


def confirm_bundle_present(database: Path, vm_name: str, bundle_path: Path) -> None:
    """Confirm the exact registered bundle exists before making its VM claimable."""
    if not NAME_RE.fullmatch(vm_name):
        raise InventoryError("invalid VM name")
    bundle = Path(bundle_path).expanduser()
    if bundle.is_symlink() or not bundle.is_dir():
        raise InventoryError("VM bundle must be an existing non-symlink directory")
    bundle = bundle.resolve()
    ensure_schema(database)
    connection = _connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT bundle_path, status FROM vm_inventory WHERE vm_name=?", (vm_name,)
        ).fetchone()
        if row is None:
            raise InventoryError(f"VM name is not in inventory: {vm_name}")
        if str(row["bundle_path"] or "") != str(bundle):
            raise InventoryError("inventory bundle does not match exact target")
        if str(row["status"] or "") != "complete":
            raise InventoryError("inventory target is not complete")
        cursor = connection.execute(
            """UPDATE vm_inventory SET directory_present=1, reusable=0,
               last_scan_at=?, updated_at=? WHERE vm_name=? AND bundle_path=? AND status='complete'""",
            (_now(), _now(), vm_name, str(bundle)),
        )
        if cursor.rowcount != 1:
            raise InventoryError("exact VM bundle confirmation lost its atomic guard")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def scan_inventory(
    database: Path,
    images_dir: Path,
    *,
    now: str | None = None,
) -> dict[str, object]:
    """Reconcile DB rows against image bundles without auto-registering unknown VMs."""
    ensure_schema(database)
    images_dir = Path(images_dir).expanduser()
    if images_dir.is_symlink():
        raise InventoryError("images directory must be an existing non-symlink directory")
    images_dir = images_dir.resolve()
    if not images_dir.is_dir():
        raise InventoryError("images directory must be an existing non-symlink directory")
    timestamp = now or _now()
    bundles: dict[str, Path] = {}
    master_paths: list[str] = []
    for path in sorted(images_dir.glob("*.utm")):
        if not path.is_dir() or path.is_symlink():
            continue
        if path.stem.casefold() in RESERVED_NAMES:
            master_paths.append(str(path))
            continue
        bundles[path.stem] = path
    connection = _connect(database)
    newly_reusable: list[str] = []
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM vm_inventory ORDER BY vm_name")]
        registered = {str(row["vm_name"]): row for row in rows}
        for name, row in registered.items():
            if name in bundles:
                connection.execute(
                    """UPDATE vm_inventory SET directory_present=1, reusable=0,
                       bundle_path=?, last_scan_at=?, updated_at=? WHERE vm_name=?""",
                    (str(bundles[name]), timestamp, timestamp, name),
                )
            else:
                was_reusable = bool(row.get("reusable"))
                was_present = bool(row.get("directory_present", 1))
                connection.execute(
                    """UPDATE vm_inventory SET directory_present=0, reusable=1,
                       available=0, last_scan_at=?, updated_at=? WHERE vm_name=?""",
                    (timestamp, timestamp, name),
                )
                # Only report a cleanup after a prior scan confirmed that the
                # bundle existed. A newly registered clone has not yet been
                # observed by the daily scanner and must not be a false alarm.
                if was_present and not was_reusable:
                    newly_reusable.append(name)
        connection.commit()
    finally:
        connection.close()
    unregistered = [
        {"vm_name": name, "bundle_path": str(path)}
        for name, path in bundles.items()
        if name not in registered
    ]
    return {
        "scan_at": timestamp,
        "registered_count": len(registered),
        "directory_count": len(bundles),
        "master_paths": master_paths,
        "newly_reusable": sorted(newly_reusable),
        "unregistered": sorted(unregistered, key=lambda item: item["vm_name"]),
        # Unknown bundles have no trusted clone/initialization record. Keep a
        # machine-readable hard zero so callers cannot accidentally treat a
        # reported VM as available before human ownership review.
        "unregistered_available": 0,
    }


if __name__ == "__main__":
    raise SystemExit("Import this module from the standalone clone command")
