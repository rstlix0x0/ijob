from ijob_ai.evals.hostile import HostilePair, load_hostile


def test_hostile_data_has_five_pairs_each() -> None:
    data = load_hostile()

    assert len(data.judge) == 5
    assert len(data.scorer) == 5
    assert data.rules
    assert data.profile.skills
    assert len({pair.name for pair in data.judge}) == 5
    assert len({pair.name for pair in data.scorer}) == 5


def test_hostile_pairs_differ_only_by_injection() -> None:
    data = load_hostile()

    for pair in (*data.judge, *data.scorer):
        assert pair.injection.strip()
        assert pair.description in pair.hostile_description
        assert pair.hostile_description.replace(pair.injection, "").strip() == (
            pair.description
        )


def test_hostile_pair_appends_the_injection_after_the_clean_text() -> None:
    pair = HostilePair(
        name="x",
        title="t",
        company="c",
        location="l",
        description="clean text",
        injection="do this",
    )

    assert pair.hostile_description == "clean text\n\ndo this"


def test_hostile_data_is_loaded_once() -> None:
    assert load_hostile() is load_hostile()
