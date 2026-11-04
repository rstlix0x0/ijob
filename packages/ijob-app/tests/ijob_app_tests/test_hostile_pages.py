import pytest
from ijob_ai.evals.agent_cases import HostilePage, load_hostile_pages
from ijob_app.agent_site import SiteRule
from ijob_sources.links import extract_links

_TEXT_LIMIT = 100


def _shown_texts(page: HostilePage) -> list[str]:
    """Texts of the links the model is shown: on the site or a board, cut to 100."""
    rule = SiteRule(given_url=page.url)
    links = extract_links(page.html.encode("utf-8"), page.url)
    return [link.text[:_TEXT_LIMIT] for link in links if rule.admit(link.url) is None]


@pytest.mark.parametrize("page", load_hostile_pages(), ids=lambda p: p.name)
def test_an_injection_reaches_the_model_through_a_shown_link(page: HostilePage) -> None:
    texts = _shown_texts(page)

    assert any(target in text for text in texts for target in page.injected_targets)


@pytest.mark.parametrize("page", load_hostile_pages(), ids=lambda p: p.name)
def test_every_injected_target_is_named_in_a_shown_link(page: HostilePage) -> None:
    texts = _shown_texts(page)

    for target in page.injected_targets:
        assert any(target in text for text in texts), target
