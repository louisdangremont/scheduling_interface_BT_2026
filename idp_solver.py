"""Small wrapper around idp-engine for solving the schedule FO(.) program."""

from __future__ import annotations

import sys
import re
import json
from idp_engine import IDP, Theory

# windows defaults to cp1252 which crashes on idp-engine's math symbols.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SHIFTS = ["morning", "afternoon", "evening"]

# constraints always present in the theory, whatever of what user says!!
# 1) a worker can only be assigned to a shift they are available for.
# 2) every (day, shift) slot must have at least one worker assigned to it.
BUILD_IN_CONSTRAINTS = (
    "!w in Worker, d in Day, s in Shift: works(w, d, s) => is_available(w, d, s). "
    "!d in Day, s in Shift: ?w in Worker: works(w, d, s)."
)

# maps short day codes to the dates of the week as vis-timeline requires it
DATE_BY_DAY = {
    "Mon": "2026-05-04", "Tue": "2026-05-05", "Wed": "2026-05-06",
    "Thu": "2026-05-07", "Fri": "2026-05-08", "Sat": "2026-05-09",
    "Sun": "2026-05-10",
}

# same idea for all shifts; should be labeled with start and end times for vis-timeline to display them properly.
SHIFT_TIMES = {
    "morning":   {"label": "Morning",   "start": "07:00", "end": "12:00"},
    "afternoon": {"label": "Afternoon", "start": "12:00", "end": "17:00"},
    "evening":   {"label": "Evening",   "start": "17:00", "end": "22:00"},
}

# matches one "(Louis, Mon, morning)" tuple in the works predicate output.
# captures: 1=name, 2=day, 3=shift ----> in this way we can reformat the data to what vis-timeline expects. 
WORKS_TUPLE_RE = re.compile(
    r"\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)"
    r"(\s*->\s*(True|False))?"
)

# safety net in case that the llm still renders greek symbols or other non-ascii chars in the FO output; 
# we need to convert them back to ascii equivalents for idp-z3
ASCII_EQUIVALENT = [
    ("∀", "!"), ("∃", "?"), ("⇒", "=>"), ("⇔", "<=>"),
    ("∧", "&"), ("∨", "|"), ("¬", "~"), ("≠", "~="),
    ("≤", "=<"), ("≥", ">="), ("∈", "in"),
    ("Noé", "Noe"),
]


def names_from(workers: list[dict]) -> list[str]:
    return sorted({w["name"] for w in workers if w.get("name")})


# builds the vocabulary block: types + the two predicates of the schedule.
def build_vocabulary(worker_names: list[str]) -> str:
    return f"""
vocabulary V {{
    type Worker := {{{", ".join(worker_names)}}}
    type Day := {{{", ".join(DAYS)}}}
    type Shift := {{{", ".join(SHIFTS)}}}
    works:        Worker * Day * Shift -> Bool
    is_available: Worker * Day * Shift -> Bool
}}
"""


# builds the structure block: lists every (worker, day, shift) the worker
# is available for. anything not listed is automatically false 
def build_structure(workers: list[dict]) -> str:
    triples = []
    for w in workers:
        name = w.get("name")
        if not name:
            continue
        for slot in w.get("availabilities", []) or []:
            day, shift = slot.get("day"), slot.get("shift")
            if day in DAYS and shift in SHIFTS:
                triples.append(f"({name}, {day}, {shift})")
    return f"""
structure S:V {{
    is_available := {{{", ".join(triples)}}}.
}}
"""


def to_idp_syntax(fo: str) -> str:
    out = fo
    for src, dst in ASCII_EQUIVALENT:
        out = out.replace(src, dst)
    return out.strip().rstrip(".") + "."


# combines vocabulary + structure + built-in constraints + every stored FO(.) line
# into one complete idp program 
def build_program(constraints: list[dict], workers: list[dict]) -> str:
    vocab = build_vocabulary(names_from(workers))
    structure = build_structure(workers)

    theory_lines = [BUILD_IN_CONSTRAINTS]
    for c in constraints:
        if c.get("fo"):
            theory_lines.append(to_idp_syntax(c["fo"]))
    theory_body = "\n    ".join(theory_lines)

    return f"""{vocab}
{structure}

theory T:V {{
    {theory_body}
}}

procedure main() {{
    pretty_print(model_expand(T, S, max=1))
}}
"""


# asks the solver why no model exists. returns the facts + laws as text 
def explain_unsat(theory) -> str:
    facts, laws = theory.explain()
    facts_text = "\n".join(str(f) for f in facts) if facts else "(none)"
    laws_text  = "\n".join(str(law) for law in laws) if laws else "(none)"
    return f"Facts:\n{facts_text}\n\nLaws:\n{laws_text}"


# parses the raw model text into the JSON shape vis-timeline expects:
# one assignment per (date, shift), each with a list of worker names 
def parse_model_to_data(output: str, worker_names: list[str]) -> dict | None:
    s = str(output)
    cut = s.find("Model 2")
    if cut > 0:
        s = s[:cut]

    works_match = re.search(r"works\s*:=\s*\{(.*?)\}", s, re.DOTALL)
    if not works_match:
        return None

    grouped: dict[tuple[str, str], list[str]] = {}
    for m in WORKS_TUPLE_RE.finditer(works_match.group(1)):
        if m.group(5) == "False":
            continue
        worker, day, shift = m.group(1), m.group(2), m.group(3)
        if day in DATE_BY_DAY and shift in SHIFT_TIMES:
            grouped.setdefault((day, shift), []).append(worker)

    assignments = [
        {"date": DATE_BY_DAY[day], "shift": shift, "nurses": nurses}
        for (day, shift), nurses in grouped.items()
    ]
    return {
        "week_start":  "2026-05-04",
        "week_end":    "2026-05-11",
        "nurses":      sorted(set(worker_names)),
        "shifts":      SHIFT_TIMES,
        "assignments": assignments,
    }


# builds the program, asks the solver for one model, returns either
# a satisfying schedule (with vis-timeline data) or an unsat explanation 
def solve(constraints: list[dict], workers: list[dict]) -> dict:
    program = build_program(constraints, workers)

    # debug: dump the two databases + the full FO(.) program on every solve to make it easier to understand what the solver is doing
    print("\n## current workers")
    print(json.dumps(workers, indent=2, ensure_ascii=False))
    print("\n## current constraints")
    print(json.dumps(constraints, indent=2, ensure_ascii=False))
    print("\n## fo(.) program")
    print(program)

    kb = IDP.from_str(program)
    theory = Theory(
        next(iter(kb.theories.values())),
        next(iter(kb.structures.values())),
    )

    first_model = None
    for item in theory.expand(max=1, timeout_seconds=10):
        if isinstance(item, str):
            break
        first_model = item
        break

    if first_model is None:
        return {
            "ok":       True,
            "unsat":    True,
            "schedule": explain_unsat(theory),
            "program":  program,
        }

    model_str = str(first_model)
    return {
        "ok":       True,
        "schedule": model_str,
        "data":     parse_model_to_data(model_str, names_from(workers)),
        "program":  program,
    }
