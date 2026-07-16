"""Tests for scripts/ci/assemble_review_comment.py.

The assembler collects status from every CI sub-workflow into ReviewItems
classified by severity (error / action_required / warning / info), then
renders them into a single PR comment body.

Layout rules tested here:
  - errors + action_required always visible
  - warnings shown only when present
  - info in a collapsible <details> block
  - empty → clean banner
  - sections omitted when their lane was skipped (None)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "assemble_review_comment.py"
_spec = importlib.util.spec_from_file_location("assemble_review_comment", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load assemble_review_comment.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["assemble_review_comment"] = _mod
_spec.loader.exec_module(_mod)

MARKER = _mod.MARKER
ReviewItem = _mod.ReviewItem


# ─── collect_failed_jobs ─────────────────────────────────────────────


def test_failed_jobs_empty_needs():
    assert _mod.collect_failed_jobs("", "https://run") == []


def test_failed_jobs_no_failures():
    needs = json.dumps({"tests": "success", "lint": "skipped"})
    assert _mod.collect_failed_jobs(needs, "https://run") == []


def test_failed_jobs_collects_only_failures():
    needs = json.dumps({"tests": "success", "lint": "failure", "js-tests": "failure"})
    items = _mod.collect_failed_jobs(needs, "https://run/123")
    assert len(items) == 2
    assert all(i.severity == "error" for i in items)
    # sorted by name
    names = [i.title for i in items]
    assert names == ["js-tests", "lint"]
    assert all(i.link == "https://run/123" for i in items)


def test_failed_jobs_bad_json():
    assert _mod.collect_failed_jobs("not json", "https://run") == []


# ─── collect_lockfile ─────────────────────────────────────────────────


def test_lockfile_skipped():
    assert _mod.collect_lockfile(None, Path("/dev/null")) == []


def test_lockfile_no_changes():
    items = _mod.collect_lockfile(False, Path("/dev/null"))
    assert len(items) == 1
    assert items[0].severity == "info"
    assert "No lockfile changes" in items[0].summary


def test_lockfile_changed_with_content():
    diff = Path("/tmp/_test_lf.md")
    diff.write_text("#### `package-lock.json`\n\n| col | | |")
    items = _mod.collect_lockfile(True, diff)
    diff.unlink(missing_ok=True)
    assert len(items) == 1
    assert items[0].severity == "info"
    assert "dependency versions changed" in items[0].summary
    assert "#### `package-lock.json`" in items[0].detail


def test_lockfile_changed_no_content():
    items = _mod.collect_lockfile(True, Path("/nonexistent"))
    assert len(items) == 1
    assert items[0].severity == "action_required"
    assert "diff content was unavailable" in items[0].summary


# ─── collect_ci_review / collect_mcp_review ──────────────────────────


def test_ci_review_skipped():
    assert _mod.collect_ci_review(None) == []


def test_ci_review_label_present():
    items = _mod.collect_ci_review(True)
    assert len(items) == 1
    assert items[0].severity == "info"


def test_ci_review_label_missing():
    items = _mod.collect_ci_review(False)
    assert len(items) == 1
    assert items[0].severity == "action_required"
    assert "Add the `ci-reviewed` label" in items[0].summary


def test_mcp_review_skipped():
    assert _mod.collect_mcp_review(None) == []


def test_mcp_review_label_present():
    items = _mod.collect_mcp_review(True)
    assert len(items) == 1
    assert items[0].severity == "info"


def test_mcp_review_label_missing():
    items = _mod.collect_mcp_review(False)
    assert len(items) == 1
    assert items[0].severity == "action_required"
    assert "Add the `ci-reviewed` label" in items[0].summary


# ─── collect_timings ─────────────────────────────────────────────────


def test_timings_missing_file():
    assert _mod.collect_timings(Path("/nonexistent")) == []


def test_timings_info():
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "info", "summary": "All good.", "detail": "", "report_url": "https://report"}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert len(items) == 1
    assert items[0].severity == "info"
    assert items[0].link == "https://report"


def test_timings_warning():
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "warning", "summary": "Slower.", "detail": "- job: +5s", "report_url": ""}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert items[0].severity == "warning"
    assert "- job: +5s" in items[0].detail


def test_timings_error_promoted_to_info():
    """Timings is an observability job — never error severity."""
    f = Path("/tmp/_test_timings.json")
    f.write_text(json.dumps({"severity": "error", "summary": "bad", "detail": "", "report_url": ""}))
    items = _mod.collect_timings(f)
    f.unlink(missing_ok=True)
    assert items[0].severity == "info"


# ─── render_comment ───────────────────────────────────────────────────


def test_render_empty_shows_clean_banner():
    body = _mod.render_comment([])
    assert body.startswith(MARKER)
    assert "✅" in body
    assert "All checks passed" in body
    assert "###" not in body  # no section headers


def test_render_errors_always_visible():
    items = [
        ReviewItem(severity="error", title="tests", summary="Job **tests** failed.", link="https://run"),
        ReviewItem(severity="info", title="lockfile", summary="No changes."),
    ]
    body = _mod.render_comment(items)
    assert "❌ Error" in body
    assert "Job **tests** failed." in body
    assert "[View logs](https://run)" in body
    # Info goes in collapsible section
    assert "<details>" in body
    assert "No changes." in body


def test_render_action_required_visible():
    items = [
        ReviewItem(severity="action_required", title="CI review", summary="Add the label."),
    ]
    body = _mod.render_comment(items)
    assert "⚠️ Action required" in body
    assert "<details>" not in body  # no info items


def test_render_warning_shown_only_if_present():
    items = [
        ReviewItem(severity="warning", title="timings", summary="Slower."),
    ]
    body = _mod.render_comment(items)
    assert "⚠️ Warning" in body
    assert "<details>" not in body  # no info items

    # No warnings → no warning section
    items2 = [ReviewItem(severity="info", title="x", summary="y")]
    body2 = _mod.render_comment(items2)
    assert "⚠️ Warning" not in body2


def test_render_info_in_collapsible_details():
    items = [
        ReviewItem(severity="info", title="lockfile", summary="No changes."),
        ReviewItem(severity="info", title="timings", summary="OK."),
    ]
    body = _mod.render_comment(items)
    assert "<details>" in body
    assert "</details>" in body
    assert "Details (2 items)" in body
    assert "No changes." in body
    assert "OK." in body


def test_render_order_errors_then_action_then_warn_then_info():
    items = [
        ReviewItem(severity="info", title="i", summary="info"),
        ReviewItem(severity="warning", title="w", summary="warn"),
        ReviewItem(severity="action_required", title="a", summary="action"),
        ReviewItem(severity="error", title="e", summary="error"),
    ]
    body = _mod.render_comment(items)
    error_pos = body.index("❌ Error")
    action_pos = body.index("⚠️ Action required")
    warn_pos = body.index("⚠️ Warning")
    info_pos = body.index("<details>")
    assert error_pos < action_pos < warn_pos < info_pos


def test_render_multiple_errors_joined_with_divider():
    items = [
        ReviewItem(severity="error", title="tests", summary="tests failed."),
        ReviewItem(severity="error", title="lint", summary="lint failed."),
    ]
    body = _mod.render_comment(items)
    assert "tests failed." in body
    assert "lint failed." in body
    # Two errors in the same severity section, separated by ---
    error_section = body[body.index("❌ Error"):]
    assert error_section.index("---") < error_section.index("lint failed.")


# ─── assemble (integration) ──────────────────────────────────────────


def test_assemble_all_skipped_clean_banner():
    body = _mod.assemble()
    assert body.startswith(MARKER)
    assert "✅" in body
    assert "###" not in body


def test_assemble_failed_job_shown():
    needs = json.dumps({"tests": "failure", "lint": "success"})
    body = _mod.assemble(needs_json=needs, run_url="https://run/1")
    assert "❌ Error" in body
    assert "tests" in body
    assert "https://run/1" in body


def test_assemble_action_required_and_info_mixed():
    needs = json.dumps({"tests": "success"})
    body = _mod.assemble(
        needs_json=needs,
        run_url="https://run",
        lockfile_changed=False,
        ci_review=False,
        mcp_catalog=None,
    )
    # ci_review=False → action_required
    assert "⚠️ Action required" in body
    # lockfile=False → info (in collapsible)
    assert "<details>" in body
    assert "No lockfile changes" in body


def test_assemble_ci_and_mcp_independent_gating():
    """CI-review section and MCP section are gated independently."""
    body = _mod.assemble(ci_review=True, mcp_catalog=False)
    # CI-review: label present → info
    assert "`ci-reviewed` label is present" in body
    # MCP: label missing → action_required
    assert "MCP catalog" in body
    assert "⚠️ Action required" in body
    # Lockfile section not present
    assert "package-lock.json" not in body
