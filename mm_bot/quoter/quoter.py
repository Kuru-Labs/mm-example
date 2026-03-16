"""
Backward-compatibility shim.

The Quoter class has been refactored into the pluggable quoter system:
- BaseQuoter (mm_bot.quoter.base) — abstract base class
- SkewQuoter (mm_bot.quoter.skew_quoter) — the original strategy

This file preserves the old import path:
    from mm_bot.quoter.quoter import Quoter
"""
from mm_bot.quoter.skew_quoter import SkewQuoter as Quoter

__all__ = ["Quoter"]
