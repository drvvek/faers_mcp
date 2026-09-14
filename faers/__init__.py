"""FAERS MCP internals.

Split out of the original single-file server so the query construction, HTTP
client, projection and statistics can be tested against recorded fixtures
without standing up an MCP session — and so a future port has a language-
agnostic contract to be checked against.
"""

__version__ = "2.0.0"
