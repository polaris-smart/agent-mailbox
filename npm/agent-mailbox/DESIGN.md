# npm workbench launcher 0.8.2

Approved direction: publish the workbench through @polaris-smart/agent-mailbox, separate from the DeepSeek Harness plugin and unrelated unscoped name.

Use Node standard libraries with no install lifecycle script. First explicit invocation selects an exact OS/architecture Release asset, downloads it over HTTPS, verifies a SHA-256 pinned in this npm package, extracts it to a versioned per-user program cache, then forwards arguments and stdio to the actual executable. No Python dependency, credentials, agent engines, home migration, service replacement or forced shutdown. Default data-home behavior is owned by the product. An existing installed instance is handled by the product's existing single-instance contract.

Supported native targets and limitations match Release 0.8.2. Reject unsupported architectures. Native extraction: macOS ditto, Windows tar.exe, Linux tar. Verify archive before extraction. Concurrent installs use unique temporary directories and atomic final rename; existing cache is accepted only with matching receipt and executable. No shell interpolation. Diagnostics go to stderr so MCP stdout remains clean. Interrupted/failed preparation leaves no complete cache marker. Propagate child status and termination signals.

Tests: OS/CPU selection, unsupported target, checksum mismatch before extraction, failed download/extraction, cache reuse, concurrent install, paths containing spaces, native argument/stdio passthrough and actual macOS bundle version/startup. Final manifest pins hashes of public Release assets. npm pack is inspected before publish; no internal source, metadata, logs or media included. README links back to public documentation. Any npm MFA is completed by the account owner in the registry's browser flow.
