"""Made-up page bodies for the recipe analysis tests.

Nothing here is copied from a real site: the hosts, titles and addresses are
invented.
"""

import json
from typing import Final

LIST_PAGE: Final = """\
<html><head><title>Careers at Example</title></head><body>
<ul>
<li class="job"><a href="/jobs/1">Rust Engineer</a><span>Remote</span></li>
<li class="job"><a href="/jobs/2">Go Engineer</a><span>Berlin</span></li>
</ul>
</body></html>
"""
"""An HTML list of two jobs, each a `li` with a link."""

JS_ONLY_PAGE: Final = """\
<html><head><title>Careers at Example</title></head><body>
<div id="app">Loading...</div>
<footer><a href="/api/jobs/">API</a></footer>
</body></html>
"""
"""A page that renders its jobs with JavaScript: only a footer API link."""

JOBS_JSON: Final = json.dumps(
    {
        "jobs": [
            {"title": "Rust Engineer", "link": "/jobs/1", "city": "Remote"},
            {"title": "Go Engineer", "link": "/jobs/2", "city": "Berlin"},
        ]
    }
)
"""The JSON list that `JS_ONLY_PAGE`'s API link serves."""
