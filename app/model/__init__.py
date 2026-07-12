"""Character data model (Stage 1).

The record shape everything downstream reads and writes (DECISIONS.md §5):
a structured prompt (tags + selections + sliders + categorical anatomy) plus
filtered free text plus a per-character identity anchor — not an art-part rig.

Structural safety (Layer 3, §11) lands here: `Age` has no sub-20
representation, so an under-20 character cannot be constructed. The name and
free-text fields route through the Stage-0 Layer-1 filter, so a blocked
record cannot exist even after a hand-edit round-trip.
"""

from .age import MAX_AGE, MIN_AGE, Age, AgeError
from .bootstrap import (
    BootstrapCandidate,
    BootstrapManifest,
    VettedEntry,
    VettedManifest,
)
from .lora import LoraManifest
from .character import (
    CatalogEntry,
    CatalogManifest,
    CharacterRecord,
    ContentBlocked,
    Footprint,
    IdentityAnchor,
    InvalidId,
    SCHEMA_VERSION,
    ensure_safe_id,
)
from .options import (
    OptionCatalog,
    OptionFormatError,
    OptionGroup,
    OptionItem,
    load_option_catalog,
)
from .store import CharacterNotFound, CharacterStore

__all__ = [
    "Age",
    "AgeError",
    "MIN_AGE",
    "MAX_AGE",
    "CharacterRecord",
    "ContentBlocked",
    "InvalidId",
    "ensure_safe_id",
    "IdentityAnchor",
    "Footprint",
    "CatalogManifest",
    "CatalogEntry",
    "BootstrapCandidate",
    "BootstrapManifest",
    "VettedEntry",
    "VettedManifest",
    "LoraManifest",
    "SCHEMA_VERSION",
    "OptionCatalog",
    "OptionGroup",
    "OptionItem",
    "OptionFormatError",
    "load_option_catalog",
    "CharacterStore",
    "CharacterNotFound",
]
