# agent-mailbox MCP server — starts over stdio by default.
# Glama introspection: server must start and respond to MCP initialize over stdio.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Default mail root inside container; mount a volume to persist.
ENV AGENT_MAIL_HOME=/data
VOLUME ["/data"]

# stdio transport (default). Glama runs this and speaks MCP over stdio.
ENTRYPOINT ["agent-mailbox"]
