"""Frontend syntax gate (Codex AM-08: a broken bundle must never ship again).

Use Node's module parser explicitly, without executing browser-only code.
Source goes through stdin: the full bundle exceeds Linux's per-argument limit
and Windows' command-line length limit when passed to --eval.

The page could not initialise at all while this was broken, yet every Python gate
was green: a frontend syntax gate is simply missing from the six gates.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest
import tinycss2

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_JS = _ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js"


def _css_errors(source):
    errors = []

    def visit(rules):
        for rule in rules:
            if rule.type == "error":
                errors.append((rule.source_line, rule.message))
            elif rule.type == "qualified-rule":
                if any(token.type == "literal" and token.value in "{}" for token in rule.prelude):
                    errors.append((rule.source_line, "stray brace in selector"))
                for declaration in tinycss2.parse_declaration_list(rule.content):
                    if declaration.type == "error":
                        errors.append((declaration.source_line, declaration.message))
            elif rule.type == "at-rule" and rule.content is not None:
                visit(tinycss2.parse_rule_list(rule.content))

    visit(tinycss2.parse_stylesheet(source))
    return errors


def test_workbench_css_parses_without_discarded_rules():
    css = _JS.with_suffix(".css").read_text(encoding="utf-8")
    assert _css_errors(css) == []


def test_core_layout_rules_are_not_swallowed_by_a_media_block():
    # CSS parsers repair missing closing braces at EOF. A syntactically readable
    # tree can therefore still condition the entire UI on a narrow viewport.
    rules = tinycss2.parse_stylesheet(_JS.with_suffix(".css").read_text(encoding="utf-8"))
    global_selectors = {
        tinycss2.serialize(rule.prelude).strip() for rule in rules if rule.type == "qualified-rule"
    }
    assert {
        ".sidebar-scroll",
        ".project-switcher summary",
        ".stat-strip",
        ".statusbar",
    } <= global_selectors


@pytest.mark.parametrize(
    "broken",
    [
        ".sidebar { display: flex; } */",
        "@media (max-width:1100px) { .a {display:block} } } @media (max-width:760px) {.b {display:block}}",
    ],
)
def test_css_gate_rejects_the_actual_release_regressions(broken):
    assert _css_errors(broken)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available on this machine")
def test_workbench_js_has_no_syntax_error():
    """Parse the shipped frontend the way a browser/reviewer does; fail on any SyntaxError."""
    result = _parse_module(_JS.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr


def _parse_module(source):
    result = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return result


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available on this machine")
@pytest.mark.parametrize(
    "broken", ['document.querySelector("#app"));', 'const label = "use "quotes"";']
)
def test_module_gate_rejects_extra_parenthesis_and_unescaped_quotes(broken):
    result = _parse_module(broken)
    assert result.returncode != 0
    assert "SyntaxError" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available on this machine")
def test_module_gate_parses_without_executing_dom_or_top_level_await():
    assert _parse_module("await document.querySelector('#app');").returncode == 0
