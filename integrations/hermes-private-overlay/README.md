# Hermes private-work host overlay (0.8.3 candidate)

This is an opt-in, process-local compatibility overlay for one exact Hermes SDK
generation. It does not modify the installed Hermes workspace. The patched
modules are derived from Nous Research's MIT-licensed Hermes source; see
`LICENSE` and `manifest.json` for license and exact preimage/postimage hashes.

`host_overlay_boot.py` verifies all 11 existing SDK modules, the absence of the
new module, all 12 patched source files and the manifest before importing any
Hermes module. Unknown or mixed SDKs fail closed. `--probe` imports the pinned
modules in the current process and exits without starting a Gateway. The normal
launch path is not wired into the Agent Mailbox UI yet; a later controlled
cutover must select the intended profile, preserve rollback and prove the
private mailbox session cannot use the Human-facing channel.

The packaged `hermes` entry delegates to `host_overlay_boot.py`. Its basename
preserves the Gateway child identity expected by Hermes' official
`stderr_timestamp` supervisor when the child argv ends in `gateway run`.
PyInstaller collects this entry as **data**, so the App copy is not executable.
The supervised child must use the verified Hermes interpreter, for example
`<pinned-hermes-python> -I <app-resources>/hermes --host-workspace <pinned-hermes-sdk> -- gateway run --external-supervisor`.
Passing `<app-resources>/hermes` directly to `Popen` fails with permission
denied. The official supervisor recognizes the interpreter form as a Gateway
command; its isolated `--probe` has been exercised on the packaged entry.
Calling `python host_overlay_boot.py ... gateway run` directly does not meet
that supervisor's command matcher and changes its fatal-config exit behavior.
The entry is only one part of a future managed launch: the outer official
launcher, profile selection, recovery and signal forwarding still need an
isolated start/stop/restart test before any live switch.

This overlay alone is not evidence that an Agent read a message, accepted work,
delivered a reply, or that Human review is complete. Do not enable it against
a live profile solely because a probe passed.
