import re
from urllib.parse import urlsplit

from ijob_ai.evals.agent_cases import (
    HostilePage,
    Scenario,
    load_hostile_pages,
    load_scenarios,
)


def test_five_scenarios_load() -> None:
    scenarios = load_scenarios()

    assert len(scenarios) == 5
    assert all(isinstance(s, Scenario) for s in scenarios)
    assert len({s.name for s in scenarios}) == 5
    assert all(s.name.strip() and s.instruction.strip() for s in scenarios)
    assert load_scenarios() is load_scenarios()


def test_three_hostile_pages_load() -> None:
    pages = load_hostile_pages()

    assert len(pages) == 3
    assert all(isinstance(p, HostilePage) for p in pages)
    assert len({p.name for p in pages}) == 3
    for page in pages:
        assert page.url.startswith("https://")
        assert page.company.strip()
        assert page.board.strip()
        assert page.html.lstrip().startswith("<!DOCTYPE html>")
        assert page.injected_targets
    assert load_hostile_pages() is load_hostile_pages()


def _hrefs(html: str) -> set[str]:
    return set(re.findall(r'href="([^"]+)"', html))


def test_hostile_pages_hold_injected_links() -> None:
    for page in load_hostile_pages():
        links = _hrefs(page.html)

        assert page.board_url in links
        assert set(page.injected_targets) <= links
        assert page.board_url not in page.injected_targets
        assert page.company in page.html


_TEXT_LIMIT = 100


def _on_site_texts(html: str, page_url: str) -> list[str]:
    """Link texts of the page's own anchors, cut as the agent's link list cuts them."""
    site = (urlsplit(page_url).hostname or "").removeprefix("www.")
    anchors = re.findall(r'<a href="([^"]+)">([^<]*)</a>', html)
    return [
        " ".join(text.split())[:_TEXT_LIMIT]
        for href, text in anchors
        if (urlsplit(href).hostname or "").removeprefix("www.").endswith(site)
    ]


def test_hostile_pages_carry_an_injection_in_on_site_link_text() -> None:
    for page in load_hostile_pages():
        texts = _on_site_texts(page.html, page.url)

        assert any(
            target in text for text in texts for target in page.injected_targets
        ), page.name


def test_every_hostile_page_serves_its_real_board() -> None:
    for page in load_hostile_pages():
        key = page.board.split(":")[1]
        assert key in page.board_api.split("/")
        assert page.company in page.board_body
