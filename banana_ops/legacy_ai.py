"""Compatibility alias: legacy database imports now live in legacy_import."""

from .legacy_import import restore_database, stage_database

__all__ = ["restore_database", "stage_database"]
