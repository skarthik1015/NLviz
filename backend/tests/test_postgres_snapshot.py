"""Copying a Postgres database Jammi cannot read live (J33, J34): the type plan per column."""
from __future__ import annotations

import pyarrow as pa

from app.services.postgres_snapshot import _plan


def test_awkward_types_are_cast_to_what_arrow_holds():
    assert _plan("money", "money", None, None, '"v"') == ('"v"::numeric', pa.decimal128(19, 2))  # not 100x
    assert _plan("integer", "year", None, None, '"v"') == ('"v"', pa.int64())  # a domain: its base type
    assert _plan("jsonb", "jsonb", None, None, '"v"') == ('"v"::text', pa.string())
    assert _plan("USER-DEFINED", "citext", None, None, '"v"') == ('"v"::text', pa.string())
    assert _plan("ARRAY", "_text", None, None, '"v"') == ("array_to_string(\"v\", ', ')", pa.string())
    assert _plan("numeric", "numeric", 5, 2, '"v"') == ('"v"', pa.decimal128(5, 2))
    assert _plan("numeric", "numeric", None, None, '"v"') == ('"v"::double precision', pa.float64())
    assert _plan("timestamp with time zone", "timestamptz", None, None, '"v"')[1] == pa.timestamp("us", tz="UTC")
    assert _plan("bytea", "bytea", None, None, '"v"') is None  # binary: left out
