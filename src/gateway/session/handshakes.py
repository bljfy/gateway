"""Version 2 full authentication and PSK resumption; no application early data."""

from __future__ import annotations

import hmac
import math
import struct
from typing import TYPE_CHECKING

from gateway.contracts import AuthenticationError, ProtocolError
from gateway.crypto.kdf import hkdf
from gateway.protocol import (
    SUITE,
    fields,
    identity,
    parse_identity,
    read_frame,
    unfields,
    write_frame,
)
from gateway.session.store import Credential

if TYPE_CHECKING:
    from gateway.session.core import SecuritySession


class ResumeMiss(ProtocolError):
    """Only an explicit pre-business cache miss permits one new full handshake."""


async def _finish(s: SecuritySession, material: bytearray, transcript: bytes) -> None:
    key = bytes(material[40:72])
    if s.initiator:
        packet = await read_frame(s.reader)
        if not hmac.compare_digest(
            packet, b"\x04" + await s._mac(key, b"server-finished-v2", transcript)
        ):
            raise AuthenticationError("key confirmation failed")
        await write_frame(s.writer, b"\x05" + await s._mac(key, b"client-finished-v2", transcript))
    else:
        await write_frame(s.writer, b"\x04" + await s._mac(key, b"server-finished-v2", transcript))
        packet = await read_frame(s.reader)
        if not hmac.compare_digest(
            packet, b"\x05" + await s._mac(key, b"client-finished-v2", transcript)
        ):
            raise AuthenticationError("key confirmation failed")


async def initiate(s: SecuritySession) -> tuple[bytearray, bytes]:
    m = s.manager
    trust = m._trust(s.peer, outbound=True)
    s.binding = m.binding(s.peer, initiator=True)
    credential = m.store.get(s.binding) if m.store is not None and not s.force_full else None
    margin = m.request_timeout + m.policy.rotate_before_seconds
    if credential is not None and credential.expires_at - m.wall_clock() > margin:
        return await _resume_initiate(s, credential)
    nc = await m._crypto(lambda: m.backend.random_bytes(32))
    ch = b"\x01" + fields(
        b"\x02", SUITE, identity(m.local.peer), identity(s.peer), nc, bytes([m.store is not None])
    )
    await write_frame(s.writer, ch)
    sh = await read_frame(s.reader)
    if sh[:1] != b"\x02":
        raise ProtocolError("expected server hello")
    body, signature = unfields(sh[1:], 2)
    ns, sid, lifetime, timeout, ticket, expiry = unfields(body, 6)
    if (
        len(ns) != 32
        or len(sid) != 16
        or len(lifetime) != 4
        or len(timeout) != 4
        or len(expiry) != 8
        or len(ticket) not in (0, 32)
    ):
        raise ProtocolError("invalid server parameters")
    seconds, hs_timeout, expires = (
        int.from_bytes(lifetime),
        int.from_bytes(timeout),
        int.from_bytes(expiry),
    )
    if (
        not 1 <= seconds <= m.policy.absolute_lifetime_seconds
        or not 1 <= hs_timeout <= m.policy.handshake_timeout_seconds
    ):
        raise ProtocolError("unacceptable session lifetime")
    if ticket and (
        m.store is None or not m.wall_clock() < expires <= m.wall_clock() + m.resume_lifetime
    ):
        raise ProtocolError("unacceptable credential lifetime")
    await s._verify(trust, fields(b"server-hello-v2", await s._hash(ch), body), signature)
    m._reserve_id(sid)
    s.session_id, s._lifetime = sid, float(seconds)
    material = bytearray(await m._crypto(lambda: m.backend.random_bytes(104)))
    try:
        encrypted = await m._crypto(
            lambda: m.backend.encrypt_sm2(trust.encryption_public_key, bytes(material))
        )
        signature = await s._sign(
            fields(b"client-key-v2", await s._hash(fields(ch, sh)), encrypted)
        )
        ck = b"\x03" + fields(encrypted, signature)
        await write_frame(s.writer, ck)
        transcript = await s._hash(fields(ch, sh, ck))
        await _finish(s, material, transcript)
        if ticket:
            s.pending_credential = Credential(
                ticket, bytes(material[72:104]), s.binding, s.peer.peer_id, expires
            )
        return material, transcript
    except BaseException:
        material[:] = bytes(len(material))
        raise


async def accept(s: SecuritySession) -> tuple[bytearray, bytes]:
    ch = await read_frame(s.reader)
    if ch[:1] == b"\x06":
        return await _resume_accept(s, ch)
    if ch[:1] != b"\x01":
        raise ProtocolError("expected client hello")
    version, suite, sender, target, nc, capability = unfields(ch[1:], 6)
    m = s.manager
    if (
        version != b"\x02"
        or suite != SUITE
        or len(nc) != 32
        or capability not in (b"\x00", b"\x01")
    ):
        raise ProtocolError("unsupported client hello")
    if parse_identity(target) != m.local.peer:
        raise AuthenticationError("wrong recipient")
    s._peer = parse_identity(sender)
    trust = m._trust(s.peer, outbound=False)
    s.binding = m.binding(s.peer, initiator=False)
    ns, sid = await _fresh(s)
    ticket = (
        await m._crypto(lambda: m.backend.random_bytes(32))
        if m.store is not None and capability == b"\x01"
        else b""
    )
    expires = min(int(m.wall_clock()) + m.resume_lifetime, int(trust.expires_at)) if ticket else 0
    body = fields(
        ns,
        sid,
        struct.pack("!I", m.policy.absolute_lifetime_seconds),
        struct.pack("!I", m.policy.handshake_timeout_seconds),
        ticket,
        expires.to_bytes(8),
    )
    sh = b"\x02" + fields(body, await s._sign(fields(b"server-hello-v2", await s._hash(ch), body)))
    await write_frame(s.writer, sh)
    ck = await read_frame(s.reader)
    if ck[:1] != b"\x03":
        raise ProtocolError("expected client key")
    encrypted, signature = unfields(ck[1:], 2)
    await s._verify(
        trust, fields(b"client-key-v2", await s._hash(fields(ch, sh)), encrypted), signature
    )
    material = bytearray(
        await m._crypto(lambda: m.backend.decrypt_sm2(m.local.encryption_private_key, encrypted))
    )
    try:
        if len(material) != 104:
            raise AuthenticationError("invalid key material")
        transcript = await s._hash(fields(ch, sh, ck))
        await _finish(s, material, transcript)
        if ticket:
            s.pending_credential = Credential(
                ticket, bytes(material[72:104]), s.binding, s.peer.peer_id, expires
            )
        return material, transcript
    except BaseException:
        material[:] = bytes(len(material))
        raise


async def _fresh(s: SecuritySession) -> tuple[bytes, bytes]:
    m = s.manager
    ns = await m._crypto(lambda: m.backend.random_bytes(32))
    for _ in range(8):
        sid = await m._crypto(lambda: m.backend.random_bytes(16))
        if sid not in m._ids:
            m._reserve_id(sid)
            s.session_id = sid
            return ns, sid
    raise ProtocolError("session identifier collisions")


async def _material(s: SecuritySession, c: Credential, hello: bytes, body: bytes) -> bytearray:
    salt = await s._hash(fields(hello, body))
    return bytearray(
        await s.manager._crypto(
            lambda: hkdf(
                s.manager.backend, c.secret, salt, fields(b"gateway-resume-v2", c.binding), 72
            )
        )
    )


async def _resume_initiate(s: SecuritySession, c: Credential) -> tuple[bytearray, bytes]:
    s.attempted_resume = True
    m = s.manager
    nc = await m._crypto(lambda: m.backend.random_bytes(32))
    body = fields(b"\x02", SUITE, identity(m.local.peer), identity(s.peer), c.ticket, nc, c.binding)
    ch = b"\x06" + fields(body, await s._mac(c.secret, b"resume-binder-v2", body))
    await write_frame(s.writer, ch)
    sh = await read_frame(s.reader)
    if sh == b"\x09":
        raise ResumeMiss("resumption cache miss")
    if sh[:1] != b"\x07":
        raise ProtocolError("expected resume confirmation")
    server, mac = unfields(sh[1:], 2)
    ns, sid, lifetime = unfields(server, 3)
    if len(ns) != 32 or len(sid) != 16 or len(lifetime) != 4:
        raise ProtocolError("invalid resume parameters")
    seconds = int.from_bytes(lifetime)
    if (
        not 1
        <= seconds
        <= min(m.policy.absolute_lifetime_seconds, math.ceil(c.expires_at - m.wall_clock()))
    ):
        raise AuthenticationError("expired resume credential")
    material = await _material(s, c, ch, server)
    try:
        transcript = await s._hash(fields(ch, server))
        expected = await s._mac(bytes(material[40:72]), b"resume-server-v2", transcript)
        if not hmac.compare_digest(mac, expected):
            raise AuthenticationError("resume authentication failed")
        m._reserve_id(sid)
        s.session_id, s._lifetime = sid, float(seconds)
        await write_frame(
            s.writer,
            b"\x08" + await s._mac(bytes(material[40:72]), b"resume-client-v2", transcript),
        )
        s.resumed = True
        s.credential_expiry = c.expires_at
        return material, transcript
    except BaseException:
        material[:] = bytes(len(material))
        raise


async def _resume_accept(s: SecuritySession, ch: bytes) -> tuple[bytearray, bytes]:
    s.attempted_resume = True
    m = s.manager
    body, binder = unfields(ch[1:], 2)
    version, suite, sender, target, ticket, nc, binding = unfields(body, 7)
    if (
        version != b"\x02"
        or suite != SUITE
        or len(ticket) != 32
        or len(nc) != 32
        or len(binding) != 32
    ):
        raise ProtocolError("invalid resume request")
    if parse_identity(target) != m.local.peer:
        raise AuthenticationError("wrong recipient")
    s._peer = parse_identity(sender)
    m._trust(s.peer, outbound=False)
    s.binding = m.binding(s.peer, initiator=False)
    if binding != s.binding:
        raise AuthenticationError("resume context changed")
    c = m.store.get(binding, ticket) if m.store else None
    if (
        c is None
        or c.expires_at - m.wall_clock() <= m.request_timeout + m.policy.rotate_before_seconds
    ):
        await write_frame(s.writer, b"\x09")
        raise ResumeMiss("resumption cache miss")
    expected = await s._mac(c.secret, b"resume-binder-v2", body)
    if not hmac.compare_digest(binder, expected):
        raise AuthenticationError("resume authentication failed")
    ns, sid = await _fresh(s)
    seconds = min(m.policy.absolute_lifetime_seconds, int(c.expires_at - m.wall_clock()))
    server = fields(ns, sid, struct.pack("!I", seconds))
    material = await _material(s, c, ch, server)
    try:
        transcript = await s._hash(fields(ch, server))
        await write_frame(
            s.writer,
            b"\x07"
            + fields(server, await s._mac(bytes(material[40:72]), b"resume-server-v2", transcript)),
        )
        cf = await read_frame(s.reader)
        expected = b"\x08" + await s._mac(bytes(material[40:72]), b"resume-client-v2", transcript)
        if not hmac.compare_digest(cf, expected):
            raise AuthenticationError("resume authentication failed")
        s._lifetime, s.resumed = float(seconds), True
        s.credential_expiry = c.expires_at
        return material, transcript
    except BaseException:
        material[:] = bytes(len(material))
        raise
