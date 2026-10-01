"""Convert Yahoo Fantasy XML to plain Python data, and build XML request bodies.

Yahoo's JSON output wraps every list in numbered dicts, so the XML is easier
to read reliably. Collections in the XML either carry a `count` attribute or
are in LIST_TAGS; those become lists, and everything else becomes dicts.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

# Containers that are always lists, even when they hold zero or one child
# and Yahoo omits the `count` attribute.
LIST_TAGS = {
    "eligible_positions",
    "games",
    "leagues",
    "managers",
    "matchups",
    "players",
    "roster_positions",
    "stats",
    "teams",
    "transactions",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_to_data(el: ET.Element) -> Any:
    children = list(el)
    tag = _local(el.tag)
    if not children:
        text = (el.text or "").strip()
        if tag in LIST_TAGS or "count" in el.attrib:
            return []
        return text or None
    if tag in LIST_TAGS or "count" in el.attrib:
        return [element_to_data(c) for c in children]
    grouped: dict[str, list[Any]] = {}
    for child in children:
        grouped.setdefault(_local(child.tag), []).append(element_to_data(child))
    # Tags that appear once map to a value; repeated tags map to a list.
    return {k: v[0] if len(v) == 1 else v for k, v in grouped.items()}


def parse(xml_text: str) -> Any:
    """Parse a Yahoo response and return the contents of <fantasy_content>."""
    root = ET.fromstring(xml_text)
    return element_to_data(root)


def _append(parent: ET.Element, value: Any, tag: str) -> None:
    el = ET.SubElement(parent, tag)
    if isinstance(value, dict):
        for k, v in value.items():
            if v is None:
                continue
            if isinstance(v, list):
                container = ET.SubElement(el, k)
                item_tag = k[:-1] if k.endswith("s") else "item"
                for item in v:
                    _append(container, item, item_tag)
            else:
                _append(el, v, k)
    else:
        el.text = str(value)


def build(body: dict) -> str:
    """Build `<fantasy_content>` XML from a dict.

    A list under key `players` becomes `<players><player>...</player>...</players>`.
    """
    root = ET.Element("fantasy_content")
    for k, v in body.items():
        _append(root, v, k)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
