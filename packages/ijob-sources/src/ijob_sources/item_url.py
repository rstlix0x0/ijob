"""Read an item's address without ever raising.

A parser that drops an item keeps its address, when it has one, so the page
can be read again later. The item is untrusted, so the address is read only
when it is text.
"""

from collections.abc import Mapping


def url_of(item: object, *keys: str) -> str | None:
    """Return the first of `keys` that holds text in `item`, or `None`.

    Args:
        item: The item as the source gave it, of any shape.
        keys: The item's keys that may hold its address, in order.

    Returns:
        The address, or `None` when `item` is not an object or no key holds text.
    """
    if not isinstance(item, Mapping):
        return None
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None
