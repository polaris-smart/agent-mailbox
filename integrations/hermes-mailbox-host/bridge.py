"""Fixed private subprocess protocol. No shell or browser-selected paths.

Launched with the selected installed Hermes interpreter (-I -B). The workbench
owns authorization and supplies the already verified profile and credential.
Diagnostic admission changes the official PM selection only; it does not start
or reload a Gateway and cannot claim a live receiver.
"""

import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

LIMIT = 65536
OPTIONS = {"mailbox_home", "state_dir", "socket_path", "sessions", "command", "session_file"}


def execute(request):
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("invalid_request")
    operation = request.get("operation")
    if operation == "profiles":
        if set(request) != {"version", "operation", "profile"} or not isinstance(request["profile"], str):
            raise ValueError("invalid_request")
        source = Path(__file__).resolve().with_name("discovery.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_discovery", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {"state": "profile_candidates", "profiles": module.profiles(request["profile"]),
                "session_verified": False, "delivery_verified": False}
    if operation in {"grant_preview", "grant_apply"}:
        required = {"version", "operation", "profile", "options"}
        fields = {"mailbox_home", "session_key"}
        if operation == "grant_apply":
            fields |= {"expected_config_sha256", "confirmed"}
        if (set(request) != required or not isinstance(request["options"], dict)
                or set(request["options"]) != fields):
            raise ValueError("invalid_request")
        source = Path(__file__).resolve().with_name("setup.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_grant", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = module.InjectionGrant(request["profile"])
        method = adapter.preview if operation == "grant_preview" else adapter.apply
        return method(**request["options"])
    if operation in {"diagnostic_admission_preview", "diagnostic_admission_apply",
                     "diagnostic_admission_status", "diagnostic_admission_rollback"}:
        required = {"version", "operation", "profile"}
        required.add("preparation_id" if operation == "diagnostic_admission_preview" else "plan_id")
        if operation in {"diagnostic_admission_apply", "diagnostic_admission_rollback"}:
            required |= {"confirmed", "gateway_stopped"}
        if set(request) != required or not isinstance(request["profile"], str):
            raise ValueError("invalid_request")
        source = Path(__file__).resolve().with_name("setup.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_admission", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = module.DiagnosticAdmission(request["profile"])
        if operation == "diagnostic_admission_preview":
            return adapter.preview(request["preparation_id"])
        if operation == "diagnostic_admission_apply":
            return adapter.apply(request["plan_id"], confirmed=request["confirmed"],
                                 gateway_stopped=request["gateway_stopped"])
        if operation == "diagnostic_admission_rollback":
            return adapter.rollback(request["plan_id"], confirmed=request["confirmed"],
                                    gateway_stopped=request["gateway_stopped"])
        return adapter.status(request["plan_id"])
    if operation in {"upgrade_preview", "upgrade_apply", "upgrade_status", "upgrade_rollback"}:
        source = Path(__file__).resolve().with_name("setup.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_preparation", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = module.Replacement(request["profile"])
        required = {"version", "operation", "profile"}
        if operation != "upgrade_preview":
            required.add("plan_id")
        if operation in {"upgrade_apply", "upgrade_rollback"}:
            required |= {"confirmed", "gateway_stopped"}
        if set(request) != required:
            raise ValueError("invalid_request")
        if operation == "upgrade_preview":
            return adapter.preview()
        if operation == "upgrade_apply":
            return adapter.apply(request["plan_id"], confirmed=request["confirmed"],
                                 gateway_stopped=request["gateway_stopped"])
        if operation == "upgrade_rollback":
            return adapter.rollback(request["plan_id"], confirmed=request["confirmed"],
                                    gateway_stopped=request["gateway_stopped"])
        return adapter.status(request["plan_id"])
    if operation in {"pin_preview", "pin_apply", "pin_status", "pin_rollback"}:
        source = Path(__file__).resolve().with_name("setup.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_preparation", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = module.PinUpdate(request["profile"])
        if operation == "pin_preview":
            if (set(request) != {"version", "operation", "profile", "options"}
                    or not isinstance(request["options"], dict)
                    or set(request["options"]) != {"binding_id", "session_key", "candidate_fingerprint",
                                                    "candidate_session_id"}):
                raise ValueError("invalid_request")
            return adapter.preview(**request["options"])
        required = {"version", "operation", "profile", "plan_id"}
        if operation in {"pin_apply", "pin_rollback"}:
            required |= {"expected_binding_id", "confirmed", "gateway_stopped"}
        elif operation == "pin_status":
            required.add("expected_binding_id")
        if set(request) != required:
            raise ValueError("invalid_request")
        if operation == "pin_apply":
            return adapter.apply(request["plan_id"], expected_binding_id=request["expected_binding_id"],
                                 confirmed=request["confirmed"], gateway_stopped=request["gateway_stopped"])
        if operation == "pin_rollback":
            return adapter.rollback(request["plan_id"], expected_binding_id=request["expected_binding_id"],
                                    confirmed=request["confirmed"],
                                    gateway_stopped=request["gateway_stopped"])
        return adapter.status(request["plan_id"],
                              expected_binding_id=request["expected_binding_id"])
    if operation in {"mcp_command_preview", "mcp_command_apply",
                     "mcp_command_status", "mcp_command_rollback"}:
        source = Path(__file__).resolve().with_name("setup.py")
        spec = importlib.util.spec_from_file_location("mailbox_hermes_preparation", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        adapter = module.McpCommandUpdate(request["profile"])
        required = {"version", "operation", "profile"}
        if operation == "mcp_command_preview":
            required.add("new_command")
        else:
            required.add("plan_id")
        if operation in {"mcp_command_apply", "mcp_command_rollback"}:
            required |= {"confirmed", "gateway_stopped"}
        if set(request) != required:
            raise ValueError("invalid_request")
        if operation == "mcp_command_preview":
            return adapter.preview(new_command=request["new_command"])
        if operation == "mcp_command_apply":
            return adapter.apply(request["plan_id"], confirmed=request["confirmed"],
                                 gateway_stopped=request["gateway_stopped"])
        if operation == "mcp_command_rollback":
            return adapter.rollback(request["plan_id"], confirmed=request["confirmed"],
                                    gateway_stopped=request["gateway_stopped"])
        return adapter.status(request["plan_id"])
    previewing = operation in {"preview", "preview_discovery"}
    if operation not in {"preview", "preview_discovery", "apply", "status", "remove"}:
        raise ValueError("invalid_request")
    required = {"version", "operation", "profile"}
    required |= {"options"} if previewing else {"plan_id"}
    if operation in {"apply", "remove"}:
        required.add("confirmed")
    if set(request) != required or not isinstance(request["profile"], str):
        raise ValueError("invalid_request")
    if previewing:
        options = request["options"]
        fields = {"socket_path"} if operation == "preview_discovery" else OPTIONS
        if not isinstance(options, dict) or set(options) != fields:
            raise ValueError("invalid_request")
    # Explicit sibling import: isolated Python does not put cwd on sys.path.
    source = Path(__file__).resolve().with_name("setup.py")
    spec = importlib.util.spec_from_file_location("mailbox_hermes_preparation", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = module.Preparation(request["profile"])
    if previewing:
        return getattr(adapter, operation)(**options)
    if operation == "status":
        return adapter.status(request["plan_id"])
    return getattr(adapter, operation)(request["plan_id"], confirmed=request["confirmed"])


def main():
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("oversized_request")
        request = json.loads(raw)
        # Third-party output may include configuration. Only our projection exits.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = execute(request)
        response = {"version": 1, "ok": True, "result": result}
        code = 0
    except (Exception, SystemExit):  # noqa: BLE001 - redact host config and credentials
        response = {"version": 1, "ok": False, "error": "preparation_refused_or_incomplete"}
        code = 2
    sys.stdout.write(json.dumps(response) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
