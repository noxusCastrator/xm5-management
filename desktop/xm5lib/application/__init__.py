"""Persistent, Qt-free application commands shared by interactive and automated clients."""
from .service import Application, DomainError

__all__ = ["Application", "DomainError"]
