"""`python -m agent_mailbox` — delegates to the cleanup CLI (the mail root's
maintenance entry point; server startup is via `agent-mailbox` / `-m
agent_mailbox.server`)."""

from .cleanup import main

if __name__ == "__main__":
    main()
