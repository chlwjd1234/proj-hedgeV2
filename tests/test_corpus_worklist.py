"""Streaming index/parser and mutation-scoping guard tests (no archive RPC)."""
import json
import pytest
from mdlmm.corpus_worklist import iter_json_array
from mdlmm.errors import ValidationError


def test_incremental_parser_yields_distinct_swaps(tmp_path):
    rows=[{'signature':f'sig{i}','slot':1234+i,'amount':i} for i in range(300)]
    path=tmp_path/'index.json'
    path.write_text(json.dumps({'schema_version':2,'entries':rows,'footer':'ok'}))
    assert list(iter_json_array(path)) == rows


def test_incremental_parser_missing_entry_is_fail_closed(tmp_path):
    path=tmp_path/'index.json'
    path.write_text('{"entries":[{"slot":1}, {"slot":')
    with pytest.raises(ValidationError):
        list(iter_json_array(path))


def test_incremental_parser_missing_array_is_fail_closed(tmp_path):
    path=tmp_path/'index.json'
    path.write_text('{"transactions":[]}')
    with pytest.raises(ValidationError):
        list(iter_json_array(path))
