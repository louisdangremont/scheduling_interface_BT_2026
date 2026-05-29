from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import os
import json

import idp_solver

load_dotenv()
app = FastAPI()

API_KEY  = os.getenv("OPENROUTER_API_KEY", "")
MODEL_ID = os.getenv("MODEL_ID", "")  


PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "validityprotocol.txt")

# loaded at startup, edit prompts/0_validity_protocol.txt + restart to update.
with open(PROMPT_FILE, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()

UNSAT_HUMANIZER_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompts", "explaining_conflict.txt")

# loaded at startup. used when idp-z3 returns unsat and the explanation
# needs to be rewritten in natural language so that the user gets a clear description.
with open(UNSAT_HUMANIZER_PROMPT_FILE, "r", encoding="utf-8") as f:
    UNSAT_HUMANIZER_SYSTEM_PROMPT = f.read().strip()

# file paths to the constraints and workers.json file that serve as our database and will be read and written by the tool functions defined in TOOL_DEFINITIONS   
CONSTRAINTS_DB                    = os.path.join(os.path.dirname(__file__), "database", "constraints.json")
WORKERS_DB               = os.path.join(os.path.dirname(__file__), "database", "workers.json")

SCHEDULE_DAYS   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SCHEDULE_SHIFTS = ["morning", "afternoon", "evening"]

# shown to the user when the unsat-humanizer LLM call fails; avoids ever
# leaking the raw idp-z3 FO output back to a non-technical user
LLM_UNAVAILABLE_MESSAGE = (
    "No schedule could be built from your current constraints, and the "
    "explanation service is temporarily unavailable. Please try again in a "
    "moment"
)


# returns the 21 (day, shift) combinations that are the default availability for a new worker
def full_week_availability() -> list:
    return [{"day": d, "shift": s} for d in SCHEDULE_DAYS for s in SCHEDULE_SHIFTS]


# ========= LLM tool definitiosn ==============
# sent to OpenRouter on every domain expert request, each entry describes one function
# the LLM can ask the back end to call. descriptions describe when LLM should call each function

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "save_constraint",
            "description": (
                "save one new scheduling constraint to the knowledge base. "
                "only call this if the users last message is a clear constraint that can be translated to FO(.). "
                "this is case 1 in the protocol; make sure to call this for valid constraints. "
                "do not call this for incomplete or off-topic messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nl": {"type": "string", "description": "Clean one-sentence English summary of the constraint."},
                    "fo": {"type": "string", "description": "The exact FO(.) translation, must end with a period."},
                },
                "required": ["nl", "fo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_constraints",
            "description": (
                "Delete one or more constraints from the knowledge base by their numeric ids "
                "(taken from CURRENT_CONSTRAINTS). Use this when the user asks to remove a rule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}, "description": "List of constraint ids to delete."},
                },
                "required": ["ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_workers_to_team",
            "description": (
                "Add worker names to the team's vocabulary. Use this in 2 situations: "
                "1) when the user says something like 'add Sarah to the team' with no other rule (just asking to add a worker) "
                "2) alongside save_constraint, when a constraint "
                "mentions a worker who is NOT yet in CURRENT_WORKERS. "
                "Only include names that are not already in CURRENT_WORKERS. "
                "Always capitalize the first letter (if user says 'jef', treat it as 'Jef')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}, "description": "Worker names to add."},
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_workers_from_team",
            "description": (
                "Remove worker names from the team's vocabulary. Use this when the user "
                "says something like 'Jef quit', 'Louis left from the team', 'remove X'. "
                "VERY IMPORTANT! -> in the SAME reply, you MUST also call remove_constraints with "
                "the ids of every constraint in CURRENT_CONSTRAINTS that mentions this "
                "worker, otherwise the schedule solver will fail because a constraint will mention a worker who is absent from team, making the task infeasible "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"}, "description": "Worker names to remove from the team."},
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_availability",
            "description": (
                "ADD (day, shift) slots to an existing worker's availability list. "
                "Use when the user states a worker CAN WORK / IS AVAILABLE for a shift "
                "These are facts and not assignments like (X is working this day, or X has to work this day). USE FOR 'Tom is available Monday morning', "
                "'Sarah can also work Tuesday evening'. Existing slots stay just ignore duplicates. "
                "DO NOT use for 'Tom WORKS Mon morning',  that is a constraint."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_name": {"type": "string", "description": "Name of the worker (must already appear in CURRENT_WORKERS)."},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "day":   {"type": "string", "enum": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
                                "shift": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                            },
                            "required": ["day", "shift"],
                        },
                        "description": "List of {day, shift} slots to add.",
                    },
                    "nl": {
                        "type": "string",
                        "description": (
                            "ONE clean English sentence describing the worker's CURRENT TOTAL "
                            "availability AFTER this change — not just what was added. "
                            "Example: 'Lila is available every day except Sunday.' "
                            "This is what the user sees in the Knowledge Base panel."
                        ),
                    },
                },
                "required": ["worker_name", "slots", "nl"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_availability",
            "description": (
                "REMOVE specific (day, shift) slots from an existing worker's availability list. "
                "Use when the user says a worker is no longer available for a slot, or is OFF. "
                "Examples : 'Jef is no longer available Friday morning', "
                "'Louis is off Tuesdays' (in case the user only mentions a day, make sure to remvoe all the shifts on that day). "
                "If the user tries removing slots that aren't currently in the worker's availability, just ignore those and remove the ones that are there. "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_name": {"type": "string"},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "day":   {"type": "string", "enum": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
                                "shift": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                            },
                            "required": ["day", "shift"],
                        },
                    },
                    "nl": {
                        "type": "string",
                        "description": (
                            "ONE clean English sentence describing when the worker is still available after this change. Example: 'Jef is now only available Monday, Wednesday, and Friday afternoons.' "
                            "the user should have a clear idea of what shifts are left availbable for his worker!"
                        ),
                    },
                },
                "required": ["worker_name", "slots", "nl"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_availability",
            "description": (
                "REPLACE a worker's entire availability list with a new list. "
                "remove what was there and add only what is in the new list.  "
                "Use when the user gives a complete new schedule, e.g. "
                "'Jef is now only available Monday and Tuesday mornings', remove Jef's existing availability and add only the Monday and Tuesday morning slots. "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "worker_name": {"type": "string"},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "day":   {"type": "string", "enum": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
                                "shift": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
                            },
                            "required": ["day", "shift"],
                        },
                    },
                    "nl": {
                        "type": "string",
                        "description": (
                            "ONE clean English sentence describing the worker's availabilities after this change. Example: 'Jef is now only available Monday and Tuesday mornings.' "
                        ),
                    },
                },
                "required": ["worker_name", "slots", "nl"],
            },
        },
    },
]





# ========== constraints db ==================

# retrieve list of constraints from constriants.json
def load_constraints() -> list:
    if not os.path.exists(CONSTRAINTS_DB):
        return []
    with open(CONSTRAINTS_DB, "r", encoding="utf-8") as file:
        return json.load(file)

# takes list of constriants and writes them to the workers.json file
def save_constraints(constraints: list) -> None:
    # check if file exists
    os.makedirs(os.path.dirname(CONSTRAINTS_DB), exist_ok=True)
    with open(CONSTRAINTS_DB, "w", encoding="utf-8") as f:
        json.dump(constraints, f, indent=2, ensure_ascii=False)


# adds a new constraint to constraints.json with the next free id
# nl = clean english summary the user sees. fo = the FO(.) line the solver gets 
def append_constraint(nl: str, fo: str) -> dict:
    constraints = load_constraints()
    # take the current max id and add 1 or start at 1 if there are no items.
    record = {
        "id": max((c.get("id", 0) for c in constraints), default=0) + 1,
        "nl": nl,
        "fo": fo,
    }
    constraints.append(record)
    save_constraints(constraints)
    return record


# removes constraints whose id is in `ids`. returns the ids actually removed.
def delete_constraints(ids: list[int]) -> list[int]:
    ids_set = set(ids)
    constraints = load_constraints()
    removed = [c["id"] for c in constraints if c.get("id") in ids_set]
    if removed:
        save_constraints([c for c in constraints if c.get("id") not in ids_set])
    return removed


# renders the current DB as a text block the LLM reads in every prompt so it can reference existing constrains by there id in database
def constraints_snapshot() -> str:
    # skip malformed entries (no id) so a bad record on disk never reaches the LLM.
    all_constraints = [c for c in load_constraints() if c.get("id") is not None]
    if not all_constraints:
        return "\n\nCURRENT CONSTRAINTS\n(none yet)"
    lines = []
    for constraint in all_constraints:
        lines.append(f"- id {constraint['id']}: {constraint.get('nl', '')}")
        lines.append(f"  fo: {constraint.get('fo', '')}")
    return "\n\nCURRENT CONSTRAINTS\n" + "\n".join(lines)


# ========== worker.db =================

def load_workers() -> list:
    if not os.path.exists(WORKERS_DB):
        return []
    with open(WORKERS_DB, "r", encoding="utf-8") as f:
        return json.load(f).get("workers", [])


def save_workers(workers: list) -> None:
    os.makedirs(os.path.dirname(WORKERS_DB), exist_ok=True)
    workers_sorted = sorted(workers, key=lambda w: w["name"])
    with open(WORKERS_DB, "w", encoding="utf-8") as f:
        json.dump({"workers": workers_sorted}, f, indent=2, ensure_ascii=False)


# adds workers with full-week availability returns the names actually added, names already on the team are skipped -- full week = default availability for new worker
def add_workers(new_names: list) -> list:
    workers = load_workers()
    existing = {w["name"] for w in workers}
    added = []
    for name in new_names:
        # check if name is not empty and also not already in existing workers pool
        if name and name not in existing:
            workers.append({
                "name":            name,
                "availabilities":  full_week_availability(),
                "availability_nl": f"{name} is available every shift of the week.",
            })
            existing.add(name)
            added.append(name)
    if added:
        save_workers(workers)
    return added


# removes workers by name. returns names removed
def remove_workers(names: list) -> list:
    targets = set(names)
    workers = load_workers()
    removed = [w["name"] for w in workers if w["name"] in targets]
    if removed:
        save_workers([w for w in workers if w["name"] not in targets])
    return removed


# renders the current worker names as a text block to be sent over to llm 
def current_workers() -> str:
    workers = load_workers()
    if not workers:
        return "\n\nCURRENT WORKERS\n(none yet)"
    return "\n\nCURRENT WORKERS\n" + ", ".join(sorted(w["name"] for w in workers))


# ========= availability =================

# renders a (day, shift) slot as a string key for easy comparison
def day_shift_tuple(slot: dict) -> tuple:
    return (slot.get("day"), slot.get("shift"))


def format_slot(slot: dict) -> dict | None:
    day, shift = slot.get("day"), slot.get("shift")
    if day in SCHEDULE_DAYS and shift in SCHEDULE_SHIFTS:
        return {"day": day, "shift": shift}
    return None


# adds (day, shift) slots to a worker's availability and stores the new nl summary the LLM produced 
def add_availability_for(worker_name: str, slots: list, nl: str = "") -> dict:
    workers = load_workers()
    for w in workers:
        if w["name"] != worker_name:
            continue
        existing = {day_shift_tuple(s) for s in w["availabilities"]}
        for raw in slots or []:
            slot = format_slot(raw)
            if slot and day_shift_tuple(slot) not in existing:
                w["availabilities"].append(slot)
                existing.add(day_shift_tuple(slot))
        if nl:
            w["availability_nl"] = nl
        save_workers(workers)
        return {"worker_name": worker_name}
    return {"worker_name": worker_name}


# removes given (day, shift) slots from a worker's availability and updates the nl summary
def remove_availability_for(worker_name: str, slots: list, nl: str = "") -> dict:
    workers = load_workers()
    for w in workers:
        if w["name"] != worker_name:
            continue
        keys = {day_shift_tuple(c) for c in (format_slot(s) for s in (slots or [])) if c}
        w["availabilities"] = [s for s in w["availabilities"] if day_shift_tuple(s) not in keys]
        if nl:
            w["availability_nl"] = nl
        save_workers(workers)
        return {"worker_name": worker_name}
    return {"worker_name": worker_name}


# replaces a worker's entire availability list with the given slots
def set_availability_for(worker_name: str, slots: list, nl: str = "") -> dict:
    workers = load_workers()
    for w in workers:
        if w["name"] != worker_name:
            continue
        seen = set()
        new_avail = []
        for raw in slots or []:
            slot = format_slot(raw)
            if slot and day_shift_tuple(slot) not in seen:
                new_avail.append(slot)
                seen.add(day_shift_tuple(slot))
        w["availabilities"] = new_avail
        if nl:
            w["availability_nl"] = nl
        save_workers(workers)
        return {"worker_name": worker_name}
    return {"worker_name": worker_name}



def format_availability_summary(worker: dict) -> str:
    nl = (worker.get("availability_nl") or "").strip()
    if nl:
        return nl
    slots = worker.get("availabilities", [])
    if not slots:
        return "(none — cannot be scheduled)"
    return f"{len(slots)} slot(s) available"


def availability_snapshot() -> str:
    workers = load_workers()
    if not workers:
        return "\n\nCURRENT AVAILABILITY\n(no workers yet)"
    lines = [f"- {w['name']}: {format_availability_summary(w)}"
             for w in sorted(workers, key=lambda w: w["name"])]
    return "\n\nCURRENT AVAILABILITY\n" + "\n".join(lines)


# ============ LLM communication layer ================

# defines which turn it is in the conversation and what the user said
class ChatTurn(BaseModel):
    role: str
    content: str


class ProcessRequest(BaseModel):
    text: str
    history: list[ChatTurn] = []


# tool execution order. workers must exist before constraints reference them,
# and worker deletions come last so their constraints can be cleaned up first.
_TOOL_PRIORITY = {
    "add_workers_to_team":      0,
    "add_availability":         1,
    "edit_availability":        2,
    "remove_availability":      3,
    "save_constraint":          4,
    "remove_constraints":       5,
    "remove_workers_from_team": 6,
}


# sends one chat completion request to openrouter with our tools attached
# returns the assistant message dict (content +  tool_calls if there are)
async def call_openrouter_with_tools(messages: list) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={
                "model":       MODEL_ID,
                "messages":    messages,
                "tools":       TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "temperature": 0,
                # avoid very long responses that can exceed token limit if use of expensive model like opus4.7 
                "max_tokens":  2000,
            },
            # if no response in 40s --> error 
            timeout=40.0,
        )
    # catching errors openrouter occasionally returns an error envelope
    try:
        return response.json()["choices"][0]["message"]
    except Exception:
        # in case of any error (network, parsing, unexpected response), return a fallback message and no tool calls so the user at least gets a response and can try again
        return {"content": "The AI service is temporarily unavailable. Sorry for this.", "tool_calls": []}


@app.post("/api/translate")
async def translate_constraint(req: ProcessRequest) -> dict:
    system_content = (
        SYSTEM_PROMPT
        + constraints_snapshot()
        + current_workers()
        + availability_snapshot()
    )
    messages = [{"role": "system", "content": system_content}]
    for turn in req.history:
        messages.append({"role": turn.role, "content": turn.content})
    messages.append({"role": "user", "content": req.text})

    assistant = await call_openrouter_with_tools(messages)
    tool_calls = assistant.get("tool_calls") or []

    saved_id = saved_nl = saved_fo = None
    removed_ids: list = []
    added_workers: list = []
    removed_worker_names: list = []
    availability_changes: list = []

    for tool_call in sorted(tool_calls, key=lambda tc: _TOOL_PRIORITY.get(tc["function"]["name"], 99)):
        name = tool_call["function"]["name"]
        # the LLM occasionally emits invalid JSON in the arguments string
        # skip this tool call instead of crashing the whole request!
        try:
            args = json.loads(tool_call["function"]["arguments"])

        except json.JSONDecodeError: # if error in formatting stop here
            continue

        if name == "add_workers_to_team":
            added_workers.extend(add_workers(args.get("names", [])))
        elif name == "save_constraint":
            record = append_constraint(args.get("nl", ""), args.get("fo", ""))
            saved_id, saved_nl, saved_fo = record["id"], record["nl"], record["fo"]
        elif name == "remove_constraints":
            removed_ids.extend(delete_constraints(args.get("ids", [])))
        elif name == "remove_workers_from_team":
            removed_worker_names.extend(remove_workers(args.get("names", [])))
        elif name == "add_availability":
            availability_changes.append({"op": "add",    **add_availability_for(args.get("worker_name", ""), args.get("slots", []), args.get("nl", ""))})
        elif name == "remove_availability":
            availability_changes.append({"op": "remove", **remove_availability_for(args.get("worker_name", ""), args.get("slots", []), args.get("nl", ""))})
        elif name == "edit_availability":
            availability_changes.append({"op": "edit",   **set_availability_for(args.get("worker_name", ""), args.get("slots", []), args.get("nl", ""))})

    # build the chat reply from what was actually applied
    if tool_calls:
        parts = []
        if added_workers:
            parts.append(f"Added to team: {', '.join(added_workers)}.")
        if saved_nl:
            parts.append(f"Saved: {saved_nl}")
            if saved_fo:
                parts.append(f"FO: {saved_fo}")
        if removed_ids:
            parts.append(f"Removed constraint id(s): {', '.join(str(i) for i in removed_ids)}.")
        if removed_worker_names:
            parts.append(f"Removed from team: {', '.join(removed_worker_names)}.")
        # (availability changes are always applied — workers are auto-created if needed)
        prose = (assistant.get("content") or "").strip()
        if prose:
            parts.append(prose)
        visible = " ".join(parts) if parts else "Done."
    else:
        visible = (assistant.get("content") or "").strip()

    return {
        "result":               visible,
        "is_valid":             saved_id is not None,
        "nl":                   saved_nl,
        "fo":                   saved_fo,
        "id":                   saved_id,
        "removed_ids":          removed_ids,
        "added_workers":        added_workers,
        "removed_workers":      removed_worker_names,
        "availability_changes": availability_changes,
    }


# =========== schedule =============

SCHEDULE_DATA = {
    # start on first january to avoid confusion 
    "week_start": "2026-01-01",
    "week_end":   "2026-01-07",
    "nurses":     [],
    "shifts": {
        # three hardcoded shifts
        "morning":   {"label": "Morning",   "start": "07:00", "end": "12:00"},
        "afternoon": {"label": "Afternoon", "start": "12:00", "end": "17:00"},
        "evening":   {"label": "Evening",   "start": "17:00", "end": "22:00"},
    },
    "assignments": [],
}

# get schedule data for vis-timelin
@app.get("/api/schedule")
async def get_schedule():
    return SCHEDULE_DATA

# get constraints data
@app.get("/api/constraints")
async def get_constraints():
    return load_constraints()

# get workers and their availabilities to show in the knowledge base panel and to send to the solver when building the schedule
@app.get("/api/availability")
async def get_availability():
    workers = load_workers()
    return [{"name": w["name"], "summary": format_availability_summary(w)}
            for w in sorted(workers, key=lambda w: w["name"])]


# sends idp-z3's  unsat report to back openrouter so an LLM can rewrite it as
# natural language. falls back to LLM_UNAVAILABLE_MESSAGE if failure so that the user is not left with an error message he doesnt understand
async def humanize_unsat_explanation(raw_explanation: str, constraints: list[dict]) -> str:
    if constraints:
        lines = [f"- id {c.get('id', '?')}: {c.get('nl') or '(no NL)'}  ||  FO: {c.get('fo') or '(no FO)'}"
                 for c in constraints]
        constraints_block = "\n".join(lines)
    else:
        constraints_block = "(no stored constraints)"

    user_message = (
        f"STORED CONSTRAINTS:\n{constraints_block}\n\n"
        f"SOLVER REPORT:\n{raw_explanation}"
    )
    messages = [
        {"role": "system", "content": UNSAT_HUMANIZER_SYSTEM_PROMPT},
        {"role": "user",   "content": user_message},
    ]

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model":       MODEL_ID,
                    "messages":    messages,
                    "temperature": 0,
                    "max_tokens":  400,
                },
                timeout=20.0,
            )
        text = response.json()["choices"][0]["message"]["content"].strip()
        return text or LLM_UNAVAILABLE_MESSAGE
    except Exception:
        return LLM_UNAVAILABLE_MESSAGE


@app.post("/api/create-schedule")
async def create_schedule():
    constraints = load_constraints()
    # the solver can crash on bad FO(.) syntax, timeouts, or internal idp-engine
    # errors. catch everything and return a clean error the frontend can render.
    try:
        result = idp_solver.solve(constraints, load_workers())
    except Exception as exc:
        return {"ok": False, "error": f"The solver crashed: {exc}"}
    if result.get("unsat"):
        result["schedule"] = await humanize_unsat_explanation(result["schedule"], constraints)
    return result


# ======== static files serving ====================

NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/")
async def serve_index():
    return FileResponse("index.html", headers=NO_CACHE)


@app.get("/style.css")
async def serve_css():
    return FileResponse("style.css", media_type="text/css", headers=NO_CACHE)


@app.get("/script.js")
async def serve_js():
    return FileResponse("script.js", media_type="application/javascript", headers=NO_CACHE)


app.mount("/vendor", StaticFiles(directory="vendor"), name="vendor")
