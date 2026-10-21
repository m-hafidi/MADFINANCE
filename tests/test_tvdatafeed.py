"""Offline tests for the TradingView feed frame handling (no sockets)."""

import json
import math

from madfinance.sources import tvdatafeed as tv


def frame(payload: str) -> str:
    """A complete ~m~N~m~ frame around ``payload``."""
    return f"~m~{len(payload)}~m~{payload}"


class TestDrain:
    def test_complete_frame_consumed(self):
        payload = json.dumps({"m": "series_completed"})
        state = {}
        rest = tv._drain(frame(payload), {}, state, lambda m: None)
        assert rest == ""
        assert state["completed"] is True

    def test_incomplete_frame_waits(self):
        payload = json.dumps({"m": "series_completed"})
        buf = frame(payload)[:-5]
        state = {}
        rest = tv._drain(buf, {}, state, lambda m: None)
        assert rest == buf
        assert "completed" not in state

    def test_leading_garbage_discarded(self):
        payload = json.dumps({"m": "series_completed"})
        state = {}
        rest = tv._drain("junk-before-header" + frame(payload), {},
                         state, lambda m: None)
        assert rest == ""
        assert state["completed"] is True

    def test_two_frames_back_to_back(self):
        p1 = json.dumps({"m": "series_completed"})
        p2 = json.dumps({"m": "symbol_error", "p": ["s", "bad"]})
        state = {}
        rest = tv._drain(frame(p1) + frame(p2) + "tail", {}, state,
                         lambda m: None)
        assert rest == "tail"
        assert state["completed"] is True

    def test_heartbeat_echoed(self):
        sent = []
        rest = tv._drain("~m~1~m~h", {}, {}, sent.append)
        assert sent == ["~m~1~m~h"]
        assert rest == ""


class TestHandlePayload:
    def test_regular_bar_six_values(self):
        bars = {"s1": []}
        payload = json.dumps({"m": "timescale_update", "p": [None, {"s1": {"s": [
            {"i": 0, "v": [1735689600, 100.0, 110.0, 95.0, 105.0, 1234.0]},
        ]}}]})
        tv._handle_payload(payload, bars, {})
        assert bars["s1"] == [[1735689600, 100.0, 110.0, 95.0, 105.0, 1234.0]]

    def test_index_bar_five_values_padded_with_nan(self):
        # MASI/MSI20 index bars carry no volume field
        bars = {"s1": []}
        payload = json.dumps({"m": "timescale_update", "p": [None, {"s1": {"s": [
            {"i": 0, "v": [1735689600, 22000.0, 22500.0, 21800.0, 22300.0]},
        ]}}]})
        tv._handle_payload(payload, bars, {})
        row = bars["s1"][0]
        assert row[:5] == [1735689600, 22000.0, 22500.0, 21800.0, 22300.0]
        assert len(row) == 6 and math.isnan(row[5])

    def test_legacy_list_rows(self):
        bars = {"s1": []}
        payload = json.dumps({"m": "timescale_update", "p": [None, {"s1": {"s": [
            [7, 1735689600, 10.0, 11.0, 9.0, 10.5],
        ]}}]})
        tv._handle_payload(payload, bars, {})
        row = bars["s1"][0]
        assert row[0] == 1735689600
        assert row[1:5] == [10.0, 11.0, 9.0, 10.5]
        assert len(row) == 6 and math.isnan(row[5])

    def test_unknown_series_and_short_rows_ignored(self):
        bars = {"s1": []}
        payload = json.dumps({"m": "timescale_update", "p": [None, {"other": {"s": [
            {"v": [1, 2, 3, 4, 5, 6]},
        ]}}]})
        tv._handle_payload(payload, bars, {})
        payload = json.dumps({"m": "timescale_update", "p": [None, {"s1": {"s": [
            {"v": [1, 2, 3, 4]},
        ]}}]})
        tv._handle_payload(payload, bars, {})
        assert bars["s1"] == []

    def test_invalid_json_ignored(self):
        assert tv._handle_payload("not-json", {}, {}) is None

    def test_completion_and_error_states(self):
        state = {}
        tv._handle_payload(json.dumps({"m": "series_completed"}), {}, state)
        assert state["completed"] is True

        state = {}
        tv._handle_payload(
            json.dumps({"m": "series_error", "p": ["sid", "boom", "detail"]}),
            {}, state)
        assert state["error"] == "boom | detail"

        state = {}
        tv._handle_payload(json.dumps({"m": "critical_error", "p": []}),
                           {}, state)
        assert state["error"] == "critical_error"


class TestBarsFrame:
    def test_sorted_deduped_normalized(self):
        rows = [
            [1736208000, 10.0, 11.0, 9.0, 10.5, 100.0],
            [1736208000, 12.0, 13.0, 11.0, 12.5, 200.0],   # duplicate ts
            [1736121600, 1.0, 2.0, 0.5, 1.5, float("nan")],
        ]
        df = tv._bars_frame(rows)
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name == "Date"
        assert len(df) == 2                                # duplicate dropped
        assert df.index.is_monotonic_increasing            # sorted by time
        assert df["Volume"].dtype == "int64"
        assert df["Volume"].iloc[0] == 0                   # NaN volume -> 0
        assert df["Close"].iloc[-1] == 10.5                # first duplicate kept
        # daily index: timestamps normalized to midnight
        assert (df.index == df.index.normalize()).all()

    def test_empty_rows(self):
        df = tv._bars_frame([])
        assert df.empty
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
