"""Image pipeline (Stage 3 — DECISIONS.md §4, §6, §7).

Stage 3a lands here: record → gated structured prompt → SDXL-derived model
call behind the swap scaffold. Later sub-stages (IP-Adapter, bootstrap+cull,
LoRA promotion, seed catalog, matting, on-demand cache) extend this package.

Safety: the assembler enforces the image-side Layer-1 gate (strict prompt
context, per-fragment provenance + assembled-string pass) and carries the
Layer-2 negative-prompt anchors; the Layer-2 content classifier attaches at
3c alongside the face-embedding cull.
"""

from .catalog import (
    CatalogCell,
    CatalogConfig,
    CatalogState,
    STATE_KEYS,
    build_cells,
    coerce_catalog_config,
    load_catalog_states,
    record_outfits,
    resolve_cell,
)
from .engine import (
    DEFAULT_IP_ADAPTER_SCALE,
    DEFAULT_LORA_SCALE,
    EngineBusy,
    EngineUnavailable,
    GenerationFailed,
    GenerationRequest,
    GenerationResult,
    ImageEngine,
    IP_ADAPTER_VARIANTS,
    IPAdapterConfig,
    MAX_LORA_SCALE,
    ReferenceUnreadable,
    SAMPLERS,
)
from .cull import (
    CandidateScore,
    ContentClassifier,
    ContentVerdict,
    CullConfig,
    CullToolkit,
    CullUnavailable,
    FaceEmbedder,
    FaceReading,
    FaceSwapper,
    QualityReading,
    QualityScorer,
    ToolkitFactory,
    coerce_cull_config,
    cull_and_rank,
    preflight_cull,
    score_candidate,
)
from .lora import (
    LoraTrainer,
    TrainConfig,
    TrainFailed,
    TrainItem,
    TrainRequest,
    TrainUnavailable,
    TrainerFactory,
    build_dataset,
    coerce_train_config,
    preflight_train,
)
from .matte import (
    MatteConfig,
    MatteFactory,
    MatteReading,
    MatteToolkit,
    MatteUnavailable,
    Matter,
    VARIANTS,
    VariantSpec,
    coerce_matte_config,
    evaluate_matte,
    preflight_matte,
)
from .prompt import AssembledPrompt, PromptAssembler, PromptBlocked, PromptPiece
from .service import ImageService, build_image_service

__all__ = [
    "AssembledPrompt",
    "PromptAssembler",
    "PromptBlocked",
    "PromptPiece",
    "ImageEngine",
    "EngineBusy",
    "EngineUnavailable",
    "GenerationFailed",
    "ReferenceUnreadable",
    "IPAdapterConfig",
    "IP_ADAPTER_VARIANTS",
    "DEFAULT_IP_ADAPTER_SCALE",
    "GenerationRequest",
    "GenerationResult",
    "SAMPLERS",
    "ImageService",
    "build_image_service",
    # Stage 3c — auto-filter
    "CullToolkit",
    "CullConfig",
    "CullUnavailable",
    "ToolkitFactory",
    "FaceEmbedder",
    "FaceReading",
    "QualityScorer",
    "QualityReading",
    "ContentClassifier",
    "ContentVerdict",
    "FaceSwapper",
    "CandidateScore",
    "score_candidate",
    "cull_and_rank",
    "coerce_cull_config",
    "preflight_cull",
    # Stage 3d — LoRA promotion
    "LoraTrainer",
    "TrainConfig",
    "TrainItem",
    "TrainRequest",
    "TrainFailed",
    "TrainUnavailable",
    "TrainerFactory",
    "build_dataset",
    "coerce_train_config",
    "preflight_train",
    # Stage 3e — seed catalog
    "CatalogCell",
    "CatalogConfig",
    "CatalogState",
    "build_cells",
    "record_outfits",
    "load_catalog_states",
    "coerce_catalog_config",
    "DEFAULT_LORA_SCALE",
    "MAX_LORA_SCALE",
    # Stage 3f — matting / keyable output
    "Matter",
    "MatteConfig",
    "MatteFactory",
    "MatteReading",
    "MatteToolkit",
    "MatteUnavailable",
    "VariantSpec",
    "VARIANTS",
    "coerce_matte_config",
    "evaluate_matte",
    "preflight_matte",
    # Stage 3g — on-demand generation + cache
    "STATE_KEYS",
    "resolve_cell",
]
