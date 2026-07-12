"""Safety subsystem.

Layer 1 (deterministic filter) lives here — see DECISIONS.md §11.
Layers 2 (model gating) and 3 (structural) attach at later stages;
Layer 4 (audit logging) lives in app.audit.
"""

from .layer1 import FilterResult, Layer1Filter, filter_name, filter_text, get_filter

__all__ = ["FilterResult", "Layer1Filter", "filter_name", "filter_text", "get_filter"]
