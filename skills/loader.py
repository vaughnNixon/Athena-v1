import os
import sys
import importlib
import logging
from pathlib import Path
from typing import List
from .base_skill import BaseSkill
from .registry import get_registry

logger = logging.getLogger("athena.skills.loader")

class RuntimeSkillLoader:
    def __init__(self, registry=None):
        self.registry = registry or get_registry()

    def load_from_package(self, package_path: Path) -> List[BaseSkill]:
        """Dynamically loads and registers skills from a given package directory."""
        loaded = []
        if not package_path.exists() or not package_path.is_dir():
            logger.warning("Skill package path %s does not exist.", package_path)
            return loaded

        parent_dir = str(package_path.parent.parent.resolve()) if package_path.parent.name == "skills" else str(package_path.parent.resolve())
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        init_file = package_path / "__init__.py"
        if init_file.exists():
            pkg_name = f"{package_path.parent.name}.{package_path.name}" if package_path.parent.name == "skills" else package_path.name
            try:
                mod = importlib.import_module(pkg_name)
                for item_name in dir(mod):
                    item = getattr(mod, item_name)
                    if isinstance(item, BaseSkill):
                        self.registry.register(item)
                        loaded.append(item)
            except Exception as exc:
                logger.error("Failed to dynamically load skill package %s: %s", pkg_name, exc)

        return loaded

    def scan_and_load(self, base_dir: Path) -> List[BaseSkill]:
        """Scans a base skills directory for all subpackages and loads them."""
        all_loaded = []
        if not base_dir.exists():
            return all_loaded

        for child in base_dir.iterdir():
            if child.is_dir() and not child.name.startswith("__") and not child.name.startswith("."):
                loaded = self.load_from_package(child)
                all_loaded.extend(loaded)
        return all_loaded
