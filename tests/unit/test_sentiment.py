import sqlite3

from data.collectors import sentiment


def test_parse_and_store_roundtrip():
    payload = {"data": [
        {"value": "64", "value_classification": "Greed", "timestamp": "1782950400", "time_until_update": "43200"},
        {"value": "25", "value_classification": "Extreme Fear", "timestamp": "1782864000"},
        {"value": "abc", "timestamp": "1"}, {"value": "500", "timestamp": "2"}, {"timestamp": "3"},
    ]}
    rows, bad = sentiment.parse(payload)
    assert bad == 3 and len(rows) == 2
    assert rows[0] == (1782950400, 64, "Greed")
    con = sqlite3.connect(":memory:")
    sentiment.store(con, rows)
    assert sentiment.load(con) == [(1782864000, 25), (1782950400, 64)]


def test_parse_garbage():
    assert sentiment.parse(None) == ([], 0)
    assert sentiment.load(sqlite3.connect(":memory:")) == []
