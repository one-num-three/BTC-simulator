"""Static guards on the browser console.

There is no JavaScript test runner in this project, and adding one would pull a
whole toolchain into a teaching repo that currently needs nothing but Python.
These checks are cheap and catch the specific regressions that mattered:

* Peer names, mismatch reasons and security-event text arrive over P2P from
  other people's nodes. Interpolating them into innerHTML let any classmate run
  script in everyone else's console -- and the first table to render a hostile
  peer name is the security page, the one meant to report the attack.
* The join page used to draw PRNG noise and label it a QR code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
APP_JS = STATIC / "app.js"
INDEX = STATIC / "index.html"
QR_JS = STATIC / "qrcode.js"


def test_static_files_exist():
    for path in (APP_JS, INDEX, QR_JS, STATIC / "styles.css"):
        assert path.exists(), path


def test_no_template_interpolation_into_inner_html():
    """innerHTML must never be assigned a template literal with a substitution."""
    source = APP_JS.read_text(encoding="utf-8")
    offenders = [
        match.group(0)
        for match in re.finditer(r"innerHTML\s*=\s*`[^`]*\$\{[^`]*`", source, re.S)
    ]
    assert not offenders, f"innerHTML built from interpolated data: {offenders[:3]}"


def test_no_inner_html_assignment_from_variables():
    source = APP_JS.read_text(encoding="utf-8")
    # Allow only clearing (`innerHTML = ""`); everything else should build nodes.
    assignments = re.findall(r"\.innerHTML\s*=\s*([^;\n]+)", source)
    bad = [value.strip() for value in assignments if value.strip() not in ('""', "''", "``")]
    assert not bad, f"unexpected innerHTML assignment: {bad}"


def test_no_html_string_concatenation_helpers():
    source = APP_JS.read_text(encoding="utf-8")
    for pattern in ("insertAdjacentHTML", "outerHTML =", "document.write"):
        assert pattern not in source, f"{pattern} reintroduces an HTML parsing path"


def test_attribute_values_are_not_interpolated_into_markup():
    """`title="${value}"` lets a quote in remote data escape the attribute."""
    source = APP_JS.read_text(encoding="utf-8")
    offenders = re.findall(r'\w+="\$\{', source)
    assert not offenders, f"attribute built by interpolation: {offenders[:3]}"


def test_qr_encoder_is_real_and_not_a_random_fill():
    source = QR_JS.read_text(encoding="utf-8")
    # A genuine encoder needs Reed-Solomon and mask evaluation; the old drawing
    # code had an xorshift PRNG and neither of these.
    assert "reedSolomon" in source
    assert "penalty" in source
    assert "FORMAT_INFO" in source

    # Strip comments before looking for the old PRNG, since the module header
    # explains what it replaced.
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"//.*", "", code)
    assert "imul" not in code, "the PRNG fill must not come back"
    assert "Math.random" not in code


def test_console_uses_server_side_admin_check():
    source = APP_JS.read_text(encoding="utf-8")
    assert "/api/session" in source, "admin state must come from the server"
    assert "administrator=true" not in source, (
        "the cosmetic query-parameter admin switch must not come back"
    )


def test_tabs_are_marked_up_as_a_tablist():
    markup = INDEX.read_text(encoding="utf-8")
    assert 'role="tablist"' in markup
    assert markup.count('role="tab"') >= 10
    assert markup.count('role="tabpanel"') >= 10
    assert 'aria-selected' in markup


def test_tables_have_scoped_headers_and_captions():
    markup = INDEX.read_text(encoding="utf-8")
    assert markup.count('scope="col"') > 20
    assert markup.count("<caption") >= 5


@pytest.mark.parametrize(
    "element_id",
    [
        "peersTable",
        "securityTable",
        "classroomTable",
        "historyTable",
        "pendingTable",
        "blocksTable",
        "blockTxTable",
        "walletsTable",
    ],
)
def test_every_table_body_referenced_by_the_script_exists(element_id: str):
    assert f'id="{element_id}"' in INDEX.read_text(encoding="utf-8")


def test_stylesheet_has_no_hardcoded_light_backgrounds():
    """Dark mode is a token swap; a literal white background defeats it."""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    offenders = re.findall(r"background:\s*#(?:fff|ffffff)\s*;", css)
    assert not offenders, f"hardcoded white background: {offenders}"
