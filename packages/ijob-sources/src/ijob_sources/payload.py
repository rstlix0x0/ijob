"""Reading a stored source payload back as a JSON object.

Every adapter keeps the payload of a job as JSON text in `SightingDraft.raw`.
`raw_object` is the one place that text is read back.
"""

import json

from ijob_sources.errors import ParseError


def raw_object(raw: str) -> dict[str, object]:
    """Read a stored payload as a JSON object.

    Args:
        raw: A payload as `SightingDraft.raw` holds it.

    Returns:
        The JSON object `raw` holds.

    Raises:
        ParseError: `raw` is not JSON, is nested too deeply to read, or is not
            a JSON object.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as error:
        reason = f"raw is not JSON: {error}"
        raise ParseError(reason) from error
    if not isinstance(payload, dict):
        reason = "raw is not a JSON object"
        raise ParseError(reason)
    return payload
