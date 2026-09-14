from __future__ import annotations

import json
import pathlib
from typing import Callable, Optional

import httpx
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def meta_total(total: int) -> dict:
    return {"meta": {"results": {"total": total, "skip": 0, "limit": 1}}, "results": []}


def count_body(rows: list[tuple[str, int]]) -> dict:
    return {"meta": {}, "results": [{"term": t, "count": c} for t, c in rows]}


class Recorder:
    """Captures every request the client makes, so tests can assert on the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def searches(self) -> list[Optional[str]]:
        return [r.url.params.get("search") for r in self.requests]

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response], recorder: Recorder
) -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    return httpx.MockTransport(_handle)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def known_pair() -> dict:
    """The recorded EMPAGLIFLOZIN x PANCREATITIS marginals."""
    return {
        "grand_total": load("marginal_grand_total.json")["meta"]["results"]["total"],
        "drug_total": load("marginal_drug.json")["meta"]["results"]["total"],
        "event_total": load("marginal_event.json")["meta"]["results"]["total"],
        "a": load("marginal_combo.json")["meta"]["results"]["total"],
    }


@pytest.fixture
def icsr() -> dict:
    return load("icsr_trimmed.json")["results"][0]


def error_of(excinfo) -> dict:
    """The {code, reason, recovery} payload a ToolError carries."""
    return json.loads(str(excinfo.value))
