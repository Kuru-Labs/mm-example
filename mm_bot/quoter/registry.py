from typing import Dict, Type

from mm_bot.quoter.base import BaseQuoter

QUOTER_REGISTRY: Dict[str, Type[BaseQuoter]] = {}


def register_quoter(name: str, cls: Type[BaseQuoter]) -> None:
    """Register a quoter class under a name (e.g., 'skew', 'always_replace')."""
    QUOTER_REGISTRY[name] = cls


def get_quoter_class(name: str) -> Type[BaseQuoter]:
    """Look up a registered quoter class by name. Raises ValueError if not found."""
    if name not in QUOTER_REGISTRY:
        available = list(QUOTER_REGISTRY.keys())
        raise ValueError(
            f"Unknown quoter type '{name}'. Available: {available}"
        )
    return QUOTER_REGISTRY[name]
