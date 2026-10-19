"""Encrypted, bounded resumption credentials. No plaintext secrets enter SQLite."""

import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from gateway.contracts import CryptoBackend, GatewayError
from gateway.crypto.kdf import hkdf
from gateway.logging import EventLogger
from gateway.protocol import fields
from gateway.session.protection import dpapi, restricted


class StoreError(GatewayError):
    """Fixed-message local persistence failure; never a recoverable cache miss."""


@dataclass(frozen=True, slots=True)
class Credential:
    ticket: bytes = field(repr=False)
    secret: bytes = field(repr=False)
    binding: bytes = field(repr=False)
    peer_id: str
    expires_at: int


def _lock(source: BinaryIO, *, release: bool = False) -> None:
    # Platform-only standard library modules are deliberately loaded at this boundary.
    source.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(source.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(source.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


class ResumptionStore:
    """One service process per directory; sync methods run only on the owning loop."""

    def __init__(self, directory: Path, master_file: Path, backend: CryptoBackend) -> None:
        self.backend = backend
        self.healthy = True
        self._master = bytearray()
        self._lock_file: BinaryIO | None = None
        self._db: sqlite3.Connection | None = None
        self._closed = False
        self._touches: dict[bytes, int] = {}
        self._touch_sequence = 0
        self._last_wall = 0.0
        try:
            restricted(directory, directory=True)
            restricted(master_file.parent, directory=True)
            restricted(master_file)
            if os.name != "nt" and master_file.resolve().is_relative_to(directory.resolve()):
                raise StoreError("master key must be outside credential storage")
            with master_file.open("rb") as source:
                raw = source.read(1025)
            if len(raw) > 1024:
                raise StoreError("invalid local master key")
            self._master = bytearray(dpapi(raw, protect=False) if os.name == "nt" else raw)
            if len(self._master) != 32:
                raise StoreError("invalid local master key")
            lock_path = directory / "owner.lock"
            if lock_path.exists():
                restricted(lock_path)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            self._lock_file = os.fdopen(fd, "r+b", buffering=0)
            if not lock_path.stat().st_size:
                self._lock_file.write(b"\x00")
            _lock(self._lock_file)
            database = directory / "credentials.sqlite3"
            restricted(database)
            if database.stat().st_size > 16 * 1024 * 1024:
                raise StoreError("credential database exceeds limit")
            self._db = sqlite3.connect(database, timeout=1)
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("SELECT ticket FROM credentials LIMIT 1")
            self._clock()
            self._entries()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, GatewayError):
            self.close()
            raise StoreError("credential storage unavailable") from None

    @classmethod
    def initialize(cls, directory: Path, master_file: Path, backend: CryptoBackend) -> None:
        """Explicit provisioning, refuses every existing target; never overwrites keys."""
        if directory.exists() or master_file.exists():
            raise StoreError("credential storage already exists")
        if master_file.resolve().is_relative_to(directory.resolve()):
            raise StoreError("master key must be outside credential storage")
        restricted(directory, create=True, directory=True)
        restricted(master_file.parent, directory=True)
        master = backend.random_bytes(32)
        with master_file.open("xb") as target:
            target.write(dpapi(master, protect=True) if os.name == "nt" else master)
        master_file.chmod(0o600)
        restricted(master_file)
        database = directory / "credentials.sqlite3"
        fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE credentials (ticket BLOB PRIMARY KEY, envelope BLOB NOT NULL)")
            db.execute("CREATE TABLE clock (singleton INTEGER PRIMARY KEY, envelope BLOB NOT NULL)")
        with cls(directory, master_file, backend) as store:
            store._clock()

    def __enter__(self) -> "ResumptionStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._closed or not self.healthy or self._db is None:
            raise StoreError("credential storage unavailable")
        return self._db

    def _seal(self, data: bytes, aad: bytes) -> bytes:
        salt = self.backend.random_bytes(32)
        material = hkdf(self.backend, bytes(self._master), salt, b"gateway-store-v2", 28)
        return salt + self.backend.seal_sm4_gcm(material[:16], material[16:], data, aad)

    def _open(self, envelope: bytes, aad: bytes) -> bytes:
        if not 48 <= len(envelope) <= 4096:
            raise StoreError("invalid credential envelope")
        material = hkdf(self.backend, bytes(self._master), envelope[:32], b"gateway-store-v2", 28)
        return self.backend.open_sm4_gcm(material[:16], material[16:], envelope[32:], aad)

    def _fail(self) -> StoreError:
        self.healthy = False
        EventLogger(__name__).error("credential_store_failed")
        return StoreError("credential storage failed")

    def _clock(self) -> int:
        try:
            now = time.time()
            if not math.isfinite(now) or now < 0:
                raise StoreError("invalid credential clock")
            db = self._connection()
            row = db.execute("SELECT envelope FROM clock WHERE singleton=1").fetchone()
            persisted = int.from_bytes(self._open(row[0], b"clock-v2")) if row else 0
            rollback = now + 1 < max(persisted, self._last_wall)
            if rollback:
                # Remove restored credentials before allowing a fresh full authentication.
                with db:
                    db.execute("DELETE FROM credentials")
                self._touches.clear()
            seconds = int(now)
            self._last_wall = now
            if row is None or seconds > persisted or rollback:
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO clock VALUES (1, ?)",
                        (self._seal(seconds.to_bytes(8), b"clock-v2"),),
                    )
            return seconds
        except (sqlite3.Error, OSError, ValueError, GatewayError):
            raise self._fail() from None

    def _entries(self) -> list[Credential]:
        db = self._connection()
        result = []
        rows = db.execute("SELECT ticket, envelope FROM credentials LIMIT 1025").fetchall()
        if len(rows) > 1024:
            raise StoreError("credential capacity exceeded")
        for ticket, envelope in rows:
            data = json.loads(self._open(envelope, fields(b"credential-v2", ticket)))
            value = Credential(
                ticket,
                bytes.fromhex(data["secret"]),
                bytes.fromhex(data["binding"]),
                data["peer_id"],
                data["expires_at"],
            )
            if (
                len(ticket) != 32
                or len(value.secret) != 32
                or len(value.binding) != 32
                or type(value.expires_at) is not int
                or type(value.peer_id) is not str
            ):
                raise StoreError("invalid credential")
            result.append(value)
        return result

    def get(self, binding: bytes, ticket: bytes | None = None) -> Credential | None:
        try:
            now = self._clock()
            entries = self._entries()
            with self._connection() as db:
                for entry in entries:
                    if entry.expires_at <= now:
                        db.execute("DELETE FROM credentials WHERE ticket=?", (entry.ticket,))
            selected = next(
                (
                    e
                    for e in reversed(entries)
                    if e.binding == binding
                    and e.expires_at > now
                    and (ticket is None or e.ticket == ticket)
                ),
                None,
            )
            if selected is not None:
                self._touch_sequence += 1
                self._touches[selected.ticket] = self._touch_sequence
            return selected
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, GatewayError):
            raise self._fail() from None

    def put(self, entry: Credential) -> None:
        try:
            self._clock()
            payload = json.dumps(
                dict(
                    secret=entry.secret.hex(),
                    binding=entry.binding.hex(),
                    peer_id=entry.peer_id,
                    expires_at=entry.expires_at,
                )
            ).encode()
            envelope = self._seal(payload, fields(b"credential-v2", entry.ticket))
            with self._connection() as db:
                self._flush_touches(db)
                db.execute(
                    "INSERT OR REPLACE INTO credentials VALUES (?, ?)", (entry.ticket, envelope)
                )
                db.execute(
                    "DELETE FROM credentials WHERE rowid NOT IN "
                    "(SELECT rowid FROM credentials ORDER BY rowid DESC LIMIT 1024)"
                )
        except (sqlite3.Error, OSError, ValueError, GatewayError):
            raise self._fail() from None

    def _flush_touches(self, db: sqlite3.Connection) -> None:
        for ticket in sorted(self._touches, key=lambda ticket: self._touches[ticket]):
            db.execute(
                "UPDATE credentials SET rowid=(SELECT COALESCE(MAX(rowid),0)+1 "
                "FROM credentials) WHERE ticket=?",
                (ticket,),
            )
        self._touches.clear()

    def status(self) -> list[dict[str, str | int]]:
        try:
            now = self._clock()
            return [
                dict(peer_id=e.peer_id, expires_at=e.expires_at)
                for e in self._entries()
                if e.expires_at > now
            ]
        except (sqlite3.Error, ValueError, TypeError, KeyError, GatewayError):
            raise self._fail() from None

    def revoke(self, peer_id: str | None = None) -> None:
        try:
            with self._connection() as db:
                if peer_id is None:
                    db.execute("DELETE FROM credentials")
                    self._touches.clear()
                else:
                    for entry in self._entries():
                        if entry.peer_id == peer_id:
                            db.execute("DELETE FROM credentials WHERE ticket=?", (entry.ticket,))
                            self._touches.pop(entry.ticket, None)
        except (sqlite3.Error, ValueError, TypeError, KeyError, GatewayError):
            raise self._fail() from None

    def close(self) -> None:
        if self._db is not None:
            if self.healthy and self._touches:
                try:
                    with self._db:
                        self._flush_touches(self._db)
                except sqlite3.Error:
                    self._fail()
            self._db.close()
            self._db = None
        if self._lock_file is not None:
            # Closing the descriptor releases its process lock, including failed acquisitions.
            self._lock_file.close()
            self._lock_file = None
        self._master[:] = bytes(len(self._master))
        self._master.clear()
        self._closed = True
        self._touches.clear()
