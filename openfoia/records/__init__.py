"""Public records adapters for external data sources.

Provides a registry of adapters that can search and fetch
public records from various sources (OpenCorporates, SEC EDGAR, etc.)
and return normalized entities compatible with the OpenFOIA Entity model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import RecordAdapter

# Adapter registry: source name -> adapter class
_REGISTRY: dict[str, type[RecordAdapter]] = {}


def register(name: str, adapter_cls: type[RecordAdapter]) -> None:
    """Register an adapter under the given source name."""
    _REGISTRY[name] = adapter_cls


def get_adapter(name: str) -> RecordAdapter:
    """Instantiate and return the adapter for the given source name.

    Raises KeyError if the source is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys()))
        raise KeyError(f"Unknown records source '{name}'. Available: {available}")
    return _REGISTRY[name]()


def list_sources() -> list[str]:
    """Return sorted list of registered source names."""
    return sorted(_REGISTRY.keys())


def _auto_register() -> None:
    """Auto-register built-in adapters."""
    from .opencorporates import OpenCorporatesAdapter
    from .sec_edgar import SECEdgarAdapter
    from .muckrock import MuckRockAdapter
    from .documentcloud import DocumentCloudAdapter
    from .usaspending import USASpendingAdapter
    from .propublica_nonprofit import ProPublicaNonprofitAdapter

    register("opencorporates", OpenCorporatesAdapter)
    register("sec", SECEdgarAdapter)
    register("muckrock", MuckRockAdapter)
    register("documentcloud", DocumentCloudAdapter)
    register("usaspending", USASpendingAdapter)
    register("nonprofits", ProPublicaNonprofitAdapter)


_auto_register()
