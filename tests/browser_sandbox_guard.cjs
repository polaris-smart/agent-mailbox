// macOS GUI browsers cannot register from the Codex seatbelt subprocess.
// Stop before a fixture or browser starts; run UI checks in a remote browser.
if (process.platform === 'darwin' && process.env.CODEX_SANDBOX && process.env.CODEX_SANDBOX !== 'none') {
  console.error('LOCAL_BROWSER_UNAVAILABLE_IN_CODEX_SANDBOX: use a remote browser');
  process.exit(2);
}
