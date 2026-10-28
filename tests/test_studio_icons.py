"""Studio icon sprite (src/apex_router/studio/icons.svg): structure, CSP safety, palette, size.

The page CSP is: default-src 'none'; script/style only with a nonce; img-src 'self'. The sprite is
inlined into ui.html and used with <use href="#id">, so it must hold no style="" attribute, no script,
no raster image, no foreignObject and no external reference. Colours come only from the studio palette.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPRITE = ROOT / "src" / "apex_router" / "studio" / "icons.svg"
NS = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"

THE_SET = (
    "agent-pi", "agent-claude", "agent-codex",
    "role-worker", "role-xval", "role-scout", "role-reviewer", "role-coordinator",
    "model-opus", "model-sonnet", "model-gpt", "model-gpt-sol", "model-local",
    "proc-serve", "proc-local-proxy", "proc-collect", "proc-daily", "proc-snapshot",
    "proc-mesh", "proc-keepwarm", "proc-drain", "proc-studio",
)

# Tokyo Night studio palette + skin + white (lower-case, 6-digit hex)
ALLOWED_COLOURS = frozenset({
    "#1a1b26", "#24283b", "#c0caf5", "#9096b5", "#e0af68", "#f7768e",
    "#7aa2f7", "#bb9af7", "#7dcfff", "#f5d0c5", "#ffffff",
})
COLOUR_ATTRS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color", "color")
NON_COLOURS = frozenset({"none", "currentcolor", "transparent"})
MAX_SYMBOL_BYTES = 1536
MAX_COLOURS_PER_SYMBOL = 6  # strict reading: 6 colours in total, skin and white included
BANNED_TAGS = ("script", "image", "foreignObject", "style", "iframe", "object", "embed", "a")


@pytest.fixture(scope="module")
def raw() -> str:
    return SPRITE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def root(raw):
    return ET.fromstring(raw)


@pytest.fixture(scope="module")
def symbols(root):
    return {s.get("id"): s for s in root.iter(f"{NS}symbol")}


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def test_sprite_is_one_svg_root(root):
    assert root.tag == f"{NS}svg"


def test_every_id_exists_once(raw, symbols):
    missing = [i for i in THE_SET if i not in symbols]
    assert not missing, f"missing symbols: {missing}"
    for i in THE_SET:
        assert raw.count(f'id="{i}"') == 1, f"id {i} not unique"


def test_no_extra_symbols(symbols):
    extra = sorted(set(symbols) - set(THE_SET))
    assert not extra, f"symbols outside THE SET: {extra}"


@pytest.mark.parametrize("sid", THE_SET)
def test_symbol_has_plain_title(symbols, sid):
    sym = symbols[sid]
    title = sym.find(f"{NS}title")
    assert title is not None, f"{sid}: no <title>"
    assert title.text and title.text.strip(), f"{sid}: empty <title>"
    assert len(title.text.strip()) <= 40


@pytest.mark.parametrize("sid", THE_SET)
def test_symbol_viewbox(symbols, sid):
    assert symbols[sid].get("viewBox") == "0 0 32 32"


@pytest.mark.parametrize("sid", THE_SET)
def test_symbol_size_limit(raw, sid):
    m = re.search(r'<symbol\b[^>]*\bid="' + re.escape(sid) + r'".*?</symbol>', raw, re.S)
    assert m, sid
    size = len(m.group(0).encode("utf-8"))
    assert size <= MAX_SYMBOL_BYTES, f"{sid}: {size} bytes > {MAX_SYMBOL_BYTES}"


def test_no_style_attribute_or_banned_tag(root):
    for el in root.iter():
        assert "style" not in el.attrib, f"style= on <{_local(el.tag)}>"
        assert _local(el.tag) not in BANNED_TAGS, f"banned <{_local(el.tag)}>"
        for k in el.attrib:
            assert not _local(k).lower().startswith("on"), f"event handler {k}"


def test_raw_text_has_no_banned_markup(raw):
    low = raw.lower()
    for bad in ("<script", "<image", "<foreignobject", "style=", "javascript:", "@import", "url(http", "<!entity"):
        assert bad not in low, bad


def test_no_external_href(root):
    for el in root.iter():
        for k in ("href", f"{XLINK}href"):
            v = el.get(k)
            if v is not None:
                assert v.startswith("#"), f"external href {v!r} on <{_local(el.tag)}>"


def _colours(el):
    for k in COLOUR_ATTRS:
        v = el.get(k)
        if v is not None:
            yield k, v.strip().lower()


def test_colours_only_from_palette(root):
    for el in root.iter():
        for k, v in _colours(el):
            if v in NON_COLOURS or v.startswith("url(#"):
                continue
            assert v in ALLOWED_COLOURS, f"<{_local(el.tag)} {k}={v!r}> not in palette"


@pytest.mark.parametrize("sid", THE_SET)
def test_colour_count_per_symbol(symbols, sid):
    used = {v for el in symbols[sid].iter() for _, v in _colours(el) if v in ALLOWED_COLOURS}
    assert len(used) <= MAX_COLOURS_PER_SYMBOL, f"{sid}: {sorted(used)}"


def test_review_sheet_inlines_current_sprite(raw):
    sheet = (ROOT / "docs" / "design" / "studio-v2" / "icons" / "sheet.html").read_text(encoding="utf-8")
    assert raw.strip() in sheet, "sheet.html is out of date: inline the current icons.svg"
    assert "<script" not in sheet.lower()


# ---- allow-lists (xval: deny-list checks missed STYLE=, url(...) and a duplicate symbol) ------------------

ALLOWED_TAGS = frozenset({"svg", "symbol", "title", "g", "path", "circle", "ellipse", "rect", "line", "polygon", "polyline"})
ALLOWED_ATTRS = frozenset({
    "xmlns", "aria-hidden", "id", "viewBox", "width", "height", "x", "y", "cx", "cy", "r", "rx", "ry",
    "d", "points", "x1", "y1", "x2", "y2", "transform",
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-opacity", "opacity"})
_VALUE_OK = re.compile(r"[A-Za-z0-9 #.,:\-()]*")


def test_only_allowed_tags_and_attributes(root):
    for el in root.iter():
        tag = _local(el.tag)
        assert tag in ALLOWED_TAGS, f"tag <{tag}> is not on the allow-list"
        for k, v in el.attrib.items():
            name = _local(k)
            assert name in ALLOWED_ATTRS, f"attribute {k!r} on <{tag}> is not on the allow-list"
            assert "url(" not in v.lower(), f"url() in {name} on <{tag}>"
            assert _VALUE_OK.fullmatch(v), f"odd characters in {name}={v!r} on <{tag}>"


def test_symbol_ids_unique_and_every_symbol_small(root):
    syms = list(root.iter(f"{NS}symbol"))
    ids = [s.get("id") for s in syms]
    assert len(ids) == len(set(ids)), "duplicate symbol id"
    assert set(ids) == set(THE_SET)
    for s in syms:
        size = len(ET.tostring(s, encoding="utf-8"))
        assert size <= MAX_SYMBOL_BYTES + 200, f"{s.get('id')}: {size} bytes"   # +200: namespace prefixes from tostring


@pytest.mark.parametrize("bad", [
    '<g STYLE = "opacity:.5"/>',
    '<path filter="url(/filters.svg#blur)" d="M0 0"/>',
    '<circle onload="x()" r="1"/>',
    '<use href="/x.svg#a"/>',
])
def test_the_allow_list_catches_known_bypasses(bad):
    doc = f'<svg xmlns="http://www.w3.org/2000/svg"><symbol id="agent-pi" viewBox="0 0 32 32">{bad}</symbol></svg>'
    el = ET.fromstring(doc)
    with pytest.raises(AssertionError):
        test_only_allowed_tags_and_attributes(el)


def test_a_duplicate_symbol_is_caught():
    one = '<symbol id="agent-pi" viewBox="0 0 32 32"><title>pi</title></symbol>'
    doc = f'<svg xmlns="http://www.w3.org/2000/svg">{one}{one}</svg>'
    with pytest.raises(AssertionError):
        test_symbol_ids_unique_and_every_symbol_small(ET.fromstring(doc))


# ---- raw text: only elements (xval: a processing instruction hid <img> from the XML checks; an HTML parser
# reads it). The sprite has no comment, PI, DOCTYPE, CDATA or entity, and text only inside <title>. ----------

_TITLE_TEXT = re.compile(r"[A-Za-z0-9 .:,()/\-]*")


def test_raw_sprite_has_only_elements(raw):
    for bad in ("<?", "<!", "]]>", "&"):
        assert bad not in raw, f"{bad!r} in the sprite"
    # every "<" opens one of the allowed tags (or closes one)
    for m in re.finditer(r"<(/?)([^\s/>]+)", raw):
        assert m.group(2) in ALLOWED_TAGS, f"<{m.group(1)}{m.group(2)}"


def test_text_only_in_titles(root):
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "title":
            assert _TITLE_TEXT.fullmatch(el.text or ""), el.text
        else:
            assert not (el.text or "").strip(), f"text in <{tag}>"
        assert not (el.tail or "").strip(), f"text after <{tag}>"


@pytest.mark.parametrize("bad", ['<?x ><img src="/x">?>', "<!-- <img src=/x> -->", "<![CDATA[<img src=/x>]]>"])
def test_raw_check_catches_hidden_markup(bad):
    doc = '<svg xmlns="http://www.w3.org/2000/svg">' + bad + '<symbol id="agent-pi" viewBox="0 0 32 32"></symbol></svg>'
    with pytest.raises(AssertionError):
        test_raw_sprite_has_only_elements(doc)
