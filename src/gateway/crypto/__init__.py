"""Real GmSSL backend; importing this package does not load native code."""

from gateway.crypto.backend import GmSSLBackend

__all__ = ["GmSSLBackend"]
