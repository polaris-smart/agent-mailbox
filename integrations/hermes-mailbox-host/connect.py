"""Explicit owner activation of an already configured Hermes mailbox receiver."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import uuid

import yaml

from setup import directory, read_file, write_new


def enable(profile, hermes, *, confirmed=False, run=subprocess.run):
    profile = directory(profile)
    config = profile / "config.yaml"
    before = read_file(config)
    value = yaml.safe_load(before)
    entry = value.get("plugins", {}).get("entries", {}).get("agent-mailbox-host", {})
    settings = entry.get("settings", {})
    directory(profile / "plugins" / "agent-mailbox-host", private=False)
    if (type(entry.get("allow_gateway_injection")) is not bool
            or settings.get("discovery_only") is True
            or not isinstance(settings.get("sessions"), dict) or not settings["sessions"]
            or not all(isinstance(settings.get(k), str) and Path(settings[k]).is_absolute()
                       for k in ("home", "state_dir", "socket_path"))):
        raise ValueError("existing_receiver_configuration_required")
    if not confirmed:
        return {"state": "preview", "will_enable": not entry["allow_gateway_injection"]}
    if entry["allow_gateway_injection"]:
        return {"state": "already_enabled", "delivery_verified": False}
    backups = directory(profile / "mailbox-connect-backups", create=True)
    backup = backups / (uuid.uuid4().hex + ".yaml")
    write_new(backup, before)
    if read_file(config) != before:
        raise ValueError("configuration_changed_before_enable")
    result = run([str(hermes), "config", "set",
                  "plugins.entries.agent-mailbox-host.allow_gateway_injection", "true"],
                 env={**os.environ, "HERMES_HOME": str(profile)},
                 capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("official_config_command_failed_exit_" + str(result.returncode))
    expected = copy.deepcopy(value)
    expected["plugins"]["entries"]["agent-mailbox-host"]["allow_gateway_injection"] = True
    if yaml.safe_load(read_file(config)) != expected:
        raise ValueError("configuration_postcondition_failed_backup_retained")
    return {"state": "enabled", "changed_only": "allow_gateway_injection",
            "backup": str(backup), "delivery_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--hermes", type=Path, required=True)
    parser.add_argument("--enable", action="store_true", help="Explicitly authorize Gateway injection")
    parser.add_argument("--probe", action="store_true", help="Read-only verification of one fresh message")
    parser.add_argument("--message-id")
    parser.add_argument("--marker")
    args = parser.parse_args()
    try:
        if args.probe:
            from probe import probe
            result = probe(args.profile, args.message_id or '', args.marker or '')
            print(json.dumps(result))
            return 0 if result['passed'] else 1
        result = enable(args.profile, args.hermes, confirmed=args.enable)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
