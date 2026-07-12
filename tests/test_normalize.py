from app.safety import normalize as norm


def test_casefold_and_whitespace():
    assert norm.normalize("  HeLLo   World ") == "hello world"


def test_nfkc_folds_fullwidth_and_stylized():
    assert norm.normalize("ｌｏｌｉ") == "loli"


def test_homoglyph_folding_cyrillic():
    # 'о' and 'а' below are Cyrillic.
    assert norm.normalize("lоli") == "loli"
    assert norm.normalize("rаpe") == "rape"


def test_homoglyph_table_covers_every_letter():
    # The old table left d/f/g/l/q/r/z unmapped; confirm each now folds.
    assert norm.normalize("peԁo") == "pedo"      # d U+0501
    assert norm.normalize("ƒaggot") == "faggot"  # f U+0192
    assert norm.normalize("niɡger") == "nigger"  # g U+0261
    assert norm.normalize("ӏoli") == "loli"      # l U+04CF
    assert norm.normalize("ɾape") == "rape"      # r U+027E
    assert norm.normalize("гape") == "rape"      # r Cyrillic ghe


def test_accent_stripping():
    assert norm.normalize("lólí") == "loli"


def test_zero_width_stripped():
    assert norm.normalize("lo​li") == "loli"
    assert norm.normalize("ra­pe") == "rape"  # soft hyphen


def test_leet_fold():
    assert norm.leet_fold(norm.normalize("l0l1")) == "loli"
    assert norm.leet_fold(norm.normalize("r@p3d")) == "raped"


def test_leet_fold_rewrites_digits():
    # This is why age regexes never run on the leet variant.
    assert norm.leet_fold("15") != "15"


def test_squeeze_collapses_stretch_keeps_doubles():
    assert norm.squeeze("looooli") == "loli"
    assert norm.squeeze("committee") == "committee"


def test_squeeze_never_touches_digits():
    assert norm.squeeze("111 years old") == "111 years old"


def test_collapse_doubles_folds_all_runs():
    assert norm.collapse_doubles("raape") == "rape"
    assert norm.collapse_doubles("looooli") == "loli"
    assert norm.collapse_doubles("niggger") == "niger"
    # symmetric fold: term and text both reduce to the same skeleton
    assert norm.collapse_doubles("preteeen") == norm.collapse_doubles("preteen")


def test_collapse_doubles_leaves_digits():
    assert norm.collapse_doubles("1500") == "1500"


def test_scan_variants_base_and_leet():
    variants = norm.scan_variants("l0li")
    assert "l0li" in variants
    assert "loli" in variants


def test_double_variants_present():
    variants = norm.double_variants("raape")
    assert "rape" in variants


def test_digit_safe_variants_keep_digits():
    for variant in norm.digit_safe_variants("aged 15"):
        assert "15" in variant


def test_leet_variants_fold_digits():
    variants = norm.leet_variants("against her w1ll")
    assert any("will" in v for v in variants)
