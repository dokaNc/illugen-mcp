# Introspection / offline-editing image (used by MCP directories such as Glama).
# Rendering needs the Windows build of IlluGen, so engine tools report that IlluGen is missing here.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY illugen_mcp ./illugen_mcp
RUN pip install --no-cache-dir .
ENTRYPOINT ["illugen-mcp"]
