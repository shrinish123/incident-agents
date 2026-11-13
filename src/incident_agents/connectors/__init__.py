"""Importing this package registers every built-in connector type."""

from . import git, newrelic, postgres, sim_sql, simulated, sumo  # noqa: F401
from .base import Connector, build_connector, register_connector

__all__ = ["Connector", "build_connector", "register_connector"]
