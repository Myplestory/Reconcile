"""Detector auto-discovery. Drop a .py file, implement BaseDetector, it's loaded."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path

from .base import BaseDetector

log = logging.getLogger(__name__)


def discover_detectors() -> dict[str, type[BaseDetector]]:
    """Scan this package for BaseDetector subclasses. Returns {name: class}.

    Any .py file in reconcile/detectors/ that defines a class inheriting
    BaseDetector (with a `name` attribute) is auto-discovered.
    """
    found: dict[str, type[BaseDetector]] = {}
    package_path = str(Path(__file__).parent)

    for importer, modname, ispkg in pkgutil.iter_modules([package_path]):
        if modname == "base" or modname.startswith("_"):
            continue
        try:
            module = importlib.import_module(f".{modname}", package=__package__)
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    inspect.isclass(attr)
                    and issubclass(attr, BaseDetector)
                    and attr is not BaseDetector
                    and hasattr(attr, "name")
                    and attr.name != "unnamed"
                ):
                    found[attr.name] = attr
                    log.debug("Discovered detector: %s (%s.%s)", attr.name, modname, attr_name)
        except Exception as e:
            log.warning("Failed to load detector module %s: %s", modname, e)

    log.info("Auto-discovered %d detector(s): %s", len(found), ", ".join(sorted(found)))
    return found
