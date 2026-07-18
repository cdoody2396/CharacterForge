"""Leaf utilities shared across packages. Imports nothing from app.*"""

from .io import atomic_write_json

__all__ = ["atomic_write_json"]
