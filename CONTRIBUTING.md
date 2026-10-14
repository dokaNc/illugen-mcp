# Contributing

Thanks for helping! Bug reports, new examples and fixes are all welcome.

## Reporting a bug

Open an issue with:

- your IlluGen version (`illugen_status` shows it) and Windows version;
- the tool call that failed and its full error message;
- the latest log from `%LOCALAPPDATA%\JangaFX\illugen\logs` if IlluGen was involved;
- if possible, the `.illugen` file (or a smaller one that reproduces the problem).

## Making a change

1. Fork, then `pip install -e ".[dev]"`.
2. Keep changes focused; match the surrounding style.
3. Run `ruff check .` and `pytest`. Tests marked `engine` need IlluGen and run only on a machine where it
   is installed: please run them locally when you touch `runner.py`, `server.py` or the engine tools.
4. Describe what you tested in the pull request.

## Node tables

`illugen_mcp/data/` holds the node catalog for IlluGen 1.2.1. If a node type or parameter is missing for
your IlluGen version, run `python -m illugen_mcp.build_catalog` and mention the version in your issue or PR.
