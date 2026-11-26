"""Find out which tab a plugin action was invoked from.

Herdr hands an action its launch context as JSON in HERDR_PLUGIN_CONTEXT_JSON (tab_id, pane, cwd, ...).
HERDR_TAB_ID is the fallback for a run from inside a pane. A malformed value is ignored rather than raised,
so the caller can still fall back to the UI-focused tab.
"""
from __future__ import annotations

import json
from typing import Mapping, Optional

CONTEXT_ENV = "HERDR_PLUGIN_CONTEXT_JSON"
TAB_ENV = "HERDR_TAB_ID"


def _clean(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def invoked_tab_id(environ: Mapping[str, str]) -> Optional[str]:
    """Tab ID from the action context, then HERDR_TAB_ID. None when neither gives one."""
    raw = environ.get(CONTEXT_ENV)
    if raw:
        try:
            context = json.loads(raw)
        except ValueError:
            context = None
        if isinstance(context, dict):
            tab_id = _clean(context.get("tab_id"))
            if tab_id:
                return tab_id
    return _clean(environ.get(TAB_ENV))
