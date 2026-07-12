"""Stage 3c — the sandbox-verifiable core: the pure cull gate + ranking, the
bootstrap/vetted manifests, config coercion, and the store helpers. No torch /
insightface / onnxruntime / cv2 / imgutils — all model work is behind fakes."""

import json
import math

import pytest

from app.config import Settings
from app.imagegen.cull import (
    ContentVerdict,
    CullConfig,
    CullToolkit,
    FaceReading,
    QualityReading,
    coerce_cull_config,
    cull_and_rank,
    preflight_cull,
    score_candidate,
)
from app.model import CharacterStore
from app.model.bootstrap import (
    BootstrapCandidate,
    BootstrapManifest,
    PHASE_PROPOSED,
    STATUS_KEPT,
    STATUS_PROPOSED,
    STATUS_REJECTED_CONTENT,
    STATUS_REJECTED_ERROR,
    STATUS_REJECTED_NO_FACE,
    STATUS_REJECTED_QUALITY,
    STATUS_REJECTED_SIMILARITY,
    VettedEntry,
    VettedManifest,
)

REF = (1.0, 0.0, 0.0, 0.0)


def emb_for_sim(s: float) -> tuple[float, ...]:
    """A unit vector whose dot product with REF is exactly s."""
    s = max(-1.0, min(1.0, s))
    return (s, math.sqrt(max(0.0, 1.0 - s * s)), 0.0, 0.0)


# -- tiny fakes for the four abstractions ------------------------------------


class _Emb:
    def __init__(self, reading=None, raises=False):
        self.reading = reading
        self.raises = raises

    def embed(self, path):
        if self.raises:
            raise ValueError("decode boom")
        return self.reading


class _Cls:
    def __init__(self, verdict=None, raises=False):
        self.verdict = verdict or ContentVerdict(blocked=False)
        self.raises = raises

    def classify(self, path):
        if self.raises:
            raise RuntimeError("classify boom")
        return self.verdict


class _Qual:
    def __init__(self, aesthetic=0.5, raises=False):
        self.aesthetic = aesthetic
        self.raises = raises

    def score(self, path):
        if self.raises:
            raise RuntimeError("quality boom")
        return QualityReading(aesthetic=self.aesthetic)


def toolkit(reading, *, verdict=None, aesthetic=0.5, emb_raises=False,
            cls_raises=False, qual_raises=False):
    return CullToolkit(
        embedder=_Emb(reading, raises=emb_raises),
        quality=_Qual(aesthetic, raises=qual_raises),
        classifier=_Cls(verdict, raises=cls_raises),
        swapper=None,
        ref_reading=FaceReading(found=True, embedding=REF),
    )


def good_reading(sim=1.0):
    return FaceReading(found=True, face_count=1, det_score=0.9,
                       area_fraction=0.3, sharpness=500.0,
                       embedding=emb_for_sim(sim))


CFG = CullConfig()


# -- score_candidate: the canonical gate -------------------------------------


def test_kept_when_all_gates_pass():
    tk = toolkit(good_reading(sim=0.8), aesthetic=0.7)
    score = score_candidate(tk, tk.ref_reading, "c1", "x.png", CFG)
    assert score.status == STATUS_KEPT
    assert abs(score.similarity - 0.8) < 1e-9
    assert score.aesthetic == 0.7


def test_content_block_dominates_even_a_perfect_face():
    tk = toolkit(good_reading(sim=1.0),
                 verdict=ContentVerdict(blocked=True, category="minors", matched="loli"))
    score = score_candidate(tk, tk.ref_reading, "c1", "x.png", CFG)
    assert score.status == STATUS_REJECTED_CONTENT
    assert score.content_category == "minors" and score.content_matched == "loli"


def test_content_block_on_a_no_face_frame():
    # A no-face frame can still trip a whole-image minor-coded tag -> content
    # runs first, so it is rejected_content, not rejected_no_face.
    tk = toolkit(FaceReading(found=False),
                 verdict=ContentVerdict(blocked=True, category="minors", matched="child"))
    score = score_candidate(tk, tk.ref_reading, "c1", "x.png", CFG)
    assert score.status == STATUS_REJECTED_CONTENT


def test_classify_exception_fails_closed_as_content_block():
    tk = toolkit(good_reading(sim=1.0), cls_raises=True)
    score = score_candidate(tk, tk.ref_reading, "c1", "x.png", CFG)
    assert score.status == STATUS_REJECTED_CONTENT
    assert score.content_category == "classifier_error"


def test_embed_exception_is_rejected_error():
    tk = toolkit(None, emb_raises=True)
    score = score_candidate(tk, tk.ref_reading, "c1", "x.png", CFG)
    assert score.status == STATUS_REJECTED_ERROR


def test_no_face_rejected():
    tk = toolkit(FaceReading(found=False, face_count=0))
    assert score_candidate(tk, tk.ref_reading, "c", "x", CFG).status == \
        STATUS_REJECTED_NO_FACE


@pytest.mark.parametrize("reading", [
    FaceReading(found=True, face_count=2, det_score=0.9, area_fraction=0.3,
                sharpness=500.0, embedding=emb_for_sim(1.0)),   # 2 faces
    FaceReading(found=True, face_count=1, det_score=0.2, area_fraction=0.3,
                sharpness=500.0, embedding=emb_for_sim(1.0)),   # low det
    FaceReading(found=True, face_count=1, det_score=0.9, area_fraction=0.01,
                sharpness=500.0, embedding=emb_for_sim(1.0)),   # tiny face
    FaceReading(found=True, face_count=1, det_score=0.9, area_fraction=0.95,
                sharpness=500.0, embedding=emb_for_sim(1.0)),   # huge face
    FaceReading(found=True, face_count=1, det_score=0.9, area_fraction=0.3,
                sharpness=10.0, embedding=emb_for_sim(1.0)),    # blurry
])
def test_quality_floor_rejects(reading):
    tk = toolkit(reading)
    assert score_candidate(tk, tk.ref_reading, "c", "x", CFG).status == \
        STATUS_REJECTED_QUALITY


def test_similarity_floor_rejects_drift():
    tk = toolkit(good_reading(sim=0.30))  # below the 0.50 floor
    score = score_candidate(tk, tk.ref_reading, "c", "x", CFG)
    assert score.status == STATUS_REJECTED_SIMILARITY
    assert abs(score.similarity - 0.30) < 1e-9


def test_aesthetic_failure_does_not_reject_on_model_frame():
    tk = toolkit(good_reading(sim=0.9), qual_raises=True)
    score = score_candidate(tk, tk.ref_reading, "c", "x", CFG)
    assert score.status == STATUS_KEPT and score.aesthetic == 0.0


# -- cull_and_rank -----------------------------------------------------------


def _kept(cid, sim, aes):
    from app.imagegen.cull import CandidateScore
    return CandidateScore(candidate_id=cid, status=STATUS_KEPT,
                          similarity=sim, aesthetic=aes)


def test_rank_orders_by_similarity_then_aesthetic():
    scores = [_kept("a", 0.6, 0.9), _kept("b", 0.8, 0.1), _kept("c", 0.8, 0.5)]
    survivors, short = cull_and_rank(scores, CullConfig(grid_size=2, floor=2))
    ranked = [s.candidate_id for s in survivors]
    assert ranked == ["c", "b", "a"]  # 0.8/0.5 > 0.8/0.1 (aesthetic tiebreak) > 0.6
    assert survivors[0].rank == 1 and survivors[2].rank == 3


def test_grid_size_marks_proposed_rest_kept():
    scores = [_kept(str(i), 0.9 - i * 0.01, 0.5) for i in range(20)]
    survivors, short = cull_and_rank(scores, CullConfig(grid_size=12, floor=15))
    proposed = [s for s in survivors if s.status == STATUS_PROPOSED]
    kept = [s for s in survivors if s.status == STATUS_KEPT]
    assert len(proposed) == 12 and len(kept) == 8
    assert short is False  # 20 >= floor 15


def test_short_when_below_floor():
    scores = [_kept(str(i), 0.9, 0.5) for i in range(5)]
    survivors, short = cull_and_rank(scores, CullConfig(grid_size=12, floor=15))
    assert short is True and len(survivors) == 5


def test_rejected_never_survive():
    from app.imagegen.cull import CandidateScore
    scores = [CandidateScore(candidate_id="r", status=STATUS_REJECTED_CONTENT),
              _kept("k", 0.9, 0.5)]
    survivors, _ = cull_and_rank(scores, CFG)
    assert [s.candidate_id for s in survivors] == ["k"]


# -- config coercion ---------------------------------------------------------


def test_coerce_cull_config_defaults(tmp_path):
    s = Settings(tmp_path / "s.json")
    cfg = coerce_cull_config(s)
    assert cfg.batch == 64 and cfg.similarity_floor == 0.5 and cfg.grid_size == 12


def test_coerce_cull_config_survives_bad_hand_edits(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"bootstrap": {
        "batch": "sixty", "similarity_floor": "Infinity", "floor": -3,
        "grid_size": 1e999, "face_swap_enabled": True,
    }}}), encoding="utf-8")
    s = Settings(path)
    cfg = coerce_cull_config(s)          # must not raise
    assert cfg.batch == 64               # "sixty" -> default
    assert cfg.similarity_floor == 0.5   # inf -> default
    assert cfg.floor == 15               # -3 (<=0) -> default
    assert cfg.grid_size == 12           # 1e999 -> default
    assert cfg.face_swap_enabled is True


def test_coerce_cull_config_clamps_batch(tmp_path):
    # Review A3: batch is the one knob with no downstream per-request
    # re-validation, so a fat-finger 1e9 must clamp to 256, not launch a
    # billion renders.
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"image_gen": {"bootstrap": {"batch": 1_000_000_000}}}),
                    encoding="utf-8")
    assert coerce_cull_config(Settings(path)).batch == 256
    path.write_text(json.dumps({"image_gen": {"bootstrap": {"batch": 500}}}),
                    encoding="utf-8")
    assert coerce_cull_config(Settings(path)).batch == 256


# -- preflight ---------------------------------------------------------------


def test_preflight_reports_missing_models(tmp_path):
    s = Settings(tmp_path / "s.json")
    assert preflight_cull(s, need_swap=False) == "face_models_missing"
    fr = tmp_path / "fr"
    (fr / "models" / "buffalo_l").mkdir(parents=True)
    (fr / "models" / "buffalo_l" / "det_10g.onnx").write_bytes(b"\0")
    (fr / "models" / "buffalo_l" / "w600k_r50.onnx").write_bytes(b"\0")
    s.set("models.image.face_recognition_dir", str(fr))
    assert preflight_cull(s, need_swap=False) == "classifier_unavailable"
    cc = tmp_path / "cc"
    cc.mkdir()
    s.set("models.image.content_classifier_dir", str(cc))
    assert preflight_cull(s, need_swap=False) is None
    assert preflight_cull(s, need_swap=True) == "swap_model_missing"
    sw = tmp_path / "inswapper.onnx"
    sw.write_bytes(b"\0")
    s.set("models.image.face_swapper_path", str(sw))
    assert preflight_cull(s, need_swap=True) is None


# -- manifests ---------------------------------------------------------------


def test_bootstrap_manifest_round_trip():
    manifest = BootstrapManifest(
        character_id="abc", phase=PHASE_PROPOSED, reference="reference/r.png",
        params={"batch_n": 4},
        candidates=[
            BootstrapCandidate(candidate_id="c1", path="bootstrap/candidates/c1.png",
                               seed=1, status=STATUS_PROPOSED, similarity=0.8,
                               quality={"aesthetic": 0.7}, rank=1),
            BootstrapCandidate(candidate_id="c2", path="bootstrap/candidates/c2.png",
                               seed=2, status=STATUS_REJECTED_SIMILARITY),
        ],
    )
    restored = BootstrapManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored.character_id == "abc" and restored.phase == PHASE_PROPOSED
    assert restored.counts_by_status() == {STATUS_PROPOSED: 1, STATUS_REJECTED_SIMILARITY: 1}
    assert restored.get("c1").similarity == 0.8
    assert restored.get("nope") is None


def test_candidate_final_path_prefers_swapped():
    c = BootstrapCandidate(candidate_id="c", path="bootstrap/candidates/c.png", seed=1)
    assert c.final_path() == "bootstrap/candidates/c.png"
    c.swapped_path = "bootstrap/swapped/c-swap.png"
    assert c.final_path() == "bootstrap/swapped/c-swap.png"


def test_vetted_manifest_round_trip():
    manifest = VettedManifest(character_id="abc", entries=[
        VettedEntry(path="vetted/vetted-01.png", source_candidate_id="c1",
                    seed=1, similarity=0.8, face_swapped=True),
    ])
    restored = VettedManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored.count == 1 and restored.entries[0].face_swapped is True


def test_manifest_ids_are_confined():
    from app.model.character import InvalidId
    with pytest.raises(InvalidId):
        BootstrapManifest(character_id="../escape")
    with pytest.raises(InvalidId):
        VettedManifest(character_id="a/b")


# -- store helpers -----------------------------------------------------------


def test_store_bootstrap_vetted_round_trip_and_absent(tmp_path):
    store = CharacterStore(tmp_path)
    assert store.load_bootstrap("cid") is None
    assert store.load_vetted("cid") is None
    store.save_bootstrap(BootstrapManifest(character_id="cid", reference="reference/r.png"))
    store.save_vetted(VettedManifest(character_id="cid"))
    assert store.load_bootstrap("cid").reference == "reference/r.png"
    assert store.load_vetted("cid").count == 0
    # paths stay under the character dir
    assert store.bootstrap_dir("cid") == store.char_dir("cid") / "bootstrap"
    assert store.vetted_dir("cid") == store.char_dir("cid") / "vetted"


def test_store_clear_bootstrap_scopes(tmp_path):
    store = CharacterStore(tmp_path)
    store.candidates_dir("cid").mkdir(parents=True)
    (store.candidates_dir("cid") / "c.png").write_bytes(b"x")
    store.vetted_dir("cid").mkdir(parents=True)
    (store.vetted_dir("cid") / "v.png").write_bytes(b"x")
    assert store.clear_bootstrap("cid", scope="vetted") is True
    assert not store.vetted_dir("cid").exists()
    assert store.bootstrap_dir("cid").exists()  # untouched
    assert store.clear_bootstrap("cid", scope="bootstrap") is True
    assert not store.bootstrap_dir("cid").exists()
    assert store.clear_bootstrap("cid", scope="all") is False  # nothing left


def test_store_bootstrap_paths_reject_crafted_ids(tmp_path):
    from app.model.character import InvalidId
    store = CharacterStore(tmp_path)
    for bad in ("../evil", "a/b", ".."):
        with pytest.raises((InvalidId, ValueError)):
            store.bootstrap_dir(bad)
