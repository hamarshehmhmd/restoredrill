"""Render drill reports: terminal text, JSON, Markdown (GitHub step summary), JUnit XML."""
from __future__ import annotations

import json
import os
import sys
from xml.sax.saxutils import escape, quoteattr

from .drill import human_age, human_size

C = {"red": "31", "green": "32", "yellow": "33", "dim": "2", "bold": "1"}


def use_color(mode: str, stream=sys.stdout) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


class Paint:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, text, *styles):
        if not self.on or not styles:
            return text
        return "".join(f"\x1b[{C[s]}m" for s in styles) + text + "\x1b[0m"


def text(rep, color=False) -> str:
    p = Paint(color)
    out = []
    i = rep.info or {}
    out.append(p("restoredrill", "bold") + "  " + (rep.backup or "?"))
    if i:
        src = i.get("server_version") or "?"
        bits = [f"{i.get('format')} dump", human_size(i.get("size", 0)), f"from Postgres {src}"]
        if rep.target:
            bits.append(f"restored into {rep.target} ({rep.engine})")
        if rep.restore_seconds is not None:
            bits.append(f"restore took {rep.restore_seconds:.2f} s")
        out.append(p("  " + " · ".join(bits), "dim"))
    out.append("")
    for r in rep.results:
        mark = p("✔", "green") if r.ok else p("✘", "red")
        line = f"  {mark} {r.name}"
        if r.detail:
            line += p("  " + r.detail, "dim" if r.ok else "red")
        out.append(line)
    if rep.restore_errors:
        out.append("")
        out.append(p("  restore errors:", "red", "bold"))
        for e in rep.restore_errors[:12]:
            out.append("    " + p("ERROR", "red") + "  " + e["error"])
            if e.get("hint"):
                out.append("           " + p("→ " + e["hint"], "yellow"))
        if len(rep.restore_errors) > 12:
            out.append(p(f"    ... and {len(rep.restore_errors) - 12} more (see --format json)", "dim"))
    for w in rep.warnings:
        out.append("  " + p("! " + w, "yellow"))
    out.append("")
    if rep.error:
        out.append(p("ERROR", "red", "bold") + "  " + rep.error)
    else:
        n_ok = sum(r.ok for r in rep.results)
        verdict = p("PASS", "green", "bold") if rep.status == "pass" else p("FAIL", "red", "bold")
        out.append(f"{verdict}  {n_ok}/{len(rep.results)} checks passed")
    return "\n".join(out)


def as_json(rep) -> str:
    return json.dumps(rep.to_dict(), indent=2, default=str)


def markdown(rep) -> str:
    i = rep.info or {}
    icon = {"pass": "✅", "fail": "❌"}.get(rep.status, "⚠️")
    lines = [f"## {icon} Restore drill: {rep.status.upper()}", ""]
    lines.append(f"- **Backup:** `{rep.backup}` ({i.get('format', '?')}, {human_size(i.get('size', 0))})")
    if i.get("server_version"):
        lines.append(f"- **Source:** Postgres {i['server_version']} · **Target:** {rep.target or '-'} ({rep.engine or '-'})")
    if rep.restore_seconds is not None:
        lines.append(f"- **Restore time:** {rep.restore_seconds:.2f} s · **Tables:** {len(rep.tables)} · "
                     f"**Rows:** {rep.rows_total:,}")
    lines += ["", "| | Check | Detail |", "|---|---|---|"]
    for r in rep.results:
        lines.append(f"| {'✅' if r.ok else '❌'} | {r.name} | {r.detail.replace('|', '/')} |")
    if rep.restore_errors:
        lines += ["", "**Restore errors**", ""]
        for e in rep.restore_errors[:20]:
            lines.append(f"- `{e['error']}`" + (f" — {e['hint']}" if e.get("hint") else ""))
    for w in rep.warnings:
        lines.append(f"> ⚠️ {w}")
    if rep.error:
        lines += ["", f"**Error:** {rep.error}"]
    return "\n".join(lines) + "\n"


def junit(rep) -> str:
    fails = sum(not r.ok for r in rep.results) + (1 if rep.error else 0)
    total = len(rep.results) + (1 if rep.error else 0)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuite name="restoredrill" tests="{total}" failures="{fails}" time="{rep.total_seconds or 0}">']
    for r in rep.results:
        out.append(f'  <testcase classname={quoteattr("restoredrill." + r.kind)} name={quoteattr(r.name)}>')
        if not r.ok:
            out.append(f'    <failure message={quoteattr(r.detail)}>{escape(r.detail)}</failure>')
        out.append("  </testcase>")
    if rep.error:
        out.append(f'  <testcase classname="restoredrill.engine" name="drill ran">'
                   f'<error message={quoteattr(rep.error)}/></testcase>')
    out.append("</testsuite>")
    return "\n".join(out) + "\n"


def inspect_text(info, hz, color=False) -> str:
    p = Paint(color)
    d = info.to_dict() if hasattr(info, "to_dict") else info
    rows = [("file", d["path"]), ("format", d["format"] + (" (gzip)" if d.get("compressed") else "")),
            ("size", human_size(d["size"])),
            ("database", d.get("dbname") or "-"),
            ("dumped from", f"Postgres {d.get('server_version') or '?'}"),
            ("pg_dump", d.get("pg_dump_version") or "?")]
    if d.get("created"):
        rows.append(("created", d["created"]))
    rows += [("tables", str(len(d.get("tables", [])))),
             ("extensions", ", ".join(d.get("extensions", [])) or "-"),
             ("roles needed", ", ".join(d.get("roles", [])) or "-")]
    if d.get("uses_restrict"):
        rows.append(("psql \\restrict", "yes (needs psql 17.6/16.10/15.14+)"))
    out = [f"  {p(k.ljust(14), 'dim')}{v}" for k, v in rows]
    for prob in d.get("problems", []):
        out.append("  " + p("! " + prob, "yellow"))
    for level, msg in hz:
        out.append("  " + (p("✘ " + msg, "red") if level == "error" else p("! " + msg, "yellow")))
    return "\n".join(out)
