"""Shared command registry dispatch, validation, and self-describing help."""
from __future__ import annotations
import difflib
from ._shared import err

def field_summary(spec):
    return {name: {k: v for k, v in value.items() if k != "default"}
            for name, value in spec.get("fields", {}).items()}

def validate(instruction, spec):
    instruction = dict(instruction or {})
    fields = spec.get("fields", {})
    problems = []
    unknown = sorted(set(instruction) - set(fields))
    for name in unknown:
        near = difflib.get_close_matches(name, fields, n=1, cutoff=.6)
        problems.append(f"Unknown field {name!r}. Did you mean {near[0]!r}?" if near
                       else f"Unknown field {name!r}.")
    for name, field in fields.items():
        if field.get("required") and not instruction.get(name):
            problems.append(f"Missing required instruction field: {name}.")
        elif name not in instruction and "default" in field:
            instruction[name] = field["default"]
    return instruction, problems

def render_help(procedure_name, commands, instruction):
    selected = (instruction or {}).get("command")
    if selected in commands:
        data = {key: value for key, value in commands[selected].items()
                if key != "handler"}
        data["command"] = selected
    else:
        data = {"procedure": procedure_name, "signature":
                f"CALL {procedure_name}(<command> VARCHAR, <instruction> VARIANT)",
                "commands": [{"command": name, "summary": spec.get("summary", ""),
                              "required": [k for k, v in spec.get("fields", {}).items()
                                           if v.get("required")]}
                             for name, spec in sorted(commands.items())]}
    from . import __version__
    return {"success": True, "command": "help", "run_id": None, "data": data,
            "error": None, "remedy": None, "next": [], "warning": None,
            "warnings": [], "revert": None, "bundle_version": __version__,
            "query_ids": [], "timestamp_before": None, "timestamp_after": None,
            "query_count": 0}

def dispatch(session, command, instruction, commands, procedure_name):
    cmd = (command or "").strip().lower()
    if cmd in ("help", "", "?"):
        return render_help(procedure_name, commands, instruction or {})
    if cmd not in commands:
        near = difflib.get_close_matches(cmd, commands, n=1, cutoff=.6)
        hint = f"Did you mean '{near[0]}'? " if near else ""
        return err(cmd or "(none)", f"Unknown command {command!r} for {procedure_name}.",
                   remedy=hint + f"Valid commands: {', '.join(sorted(commands))}. "
                   f"Run CALL {procedure_name}('help') for details.",
                   data={"valid_commands": sorted(commands)})
    inst, problems = validate(instruction, commands[cmd])
    if problems:
        return err(cmd, "; ".join(problems), remedy=f"CALL {procedure_name}('help', "
                   f"OBJECT_CONSTRUCT('command','{cmd}')) for the full field list.",
                   data={"fields": field_summary(commands[cmd])})
    return commands[cmd]["handler"](session, inst)
