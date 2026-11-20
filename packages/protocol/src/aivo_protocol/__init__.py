"""Shared contracts between the aivo backend and the GPU worker agent."""

from aivo_protocol.version import PROTOCOL_VERSION, protocol_compatible

__all__ = ["PROTOCOL_VERSION", "protocol_compatible"]
