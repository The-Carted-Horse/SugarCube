"""store.py — timestamp parsing, ingest, and the display snapshot.

Everything the device shows comes out of ``snapshot()``, and everything
that goes in has been through ``parse_time_ms``, which has to cope with
the four timestamp shapes the uploaders in the wild actually send.
"""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

from glucocube.store import (
    Store,
    UserSnapshot,
    extract_iob_cob,
    parse_time_ms,
)

MINUTE = 60 * 1000


def ago(minutes: float, now_ms: int | None = None) -> int:
    return int((now_ms or time.time() * 1000) - minutes * MINUTE)


# -------------------------------------------------------- parse_time_ms ----

def test_parse_time_ms_reads_epoch_milliseconds():
    assert parse_time_ms({"date": 1_700_000_000_000}, "date") == 1_700_000_000_000


def test_parse_time_ms_promotes_epoch_seconds():
    """Nightscout clients send both; anything under ~2e11 is seconds."""
    assert parse_time_ms({"date": 1_700_000_000}, "date") == 1_700_000_000_000


def test_parse_time_ms_reads_an_iso_string_with_z():
    doc = {"dateString": "2024-03-01T12:00:00Z"}
    expected = int(datetime(2024, 3, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_time_ms(doc, "dateString") == expected


def test_parse_time_ms_reads_an_iso_string_with_an_offset():
    doc = {"created_at": "2024-03-01T13:00:00+01:00"}
    expected = int(datetime(2024, 3, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_time_ms(doc, "created_at") == expected


def test_parse_time_ms_treats_a_naive_string_as_utc():
    doc = {"created_at": "2024-03-01T12:00:00"}
    expected = int(datetime(2024, 3, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
    assert parse_time_ms(doc, "created_at") == expected


def test_parse_time_ms_prefers_the_first_usable_key():
    doc = {"date": None, "created_at": 1_700_000_000_000, "timestamp": 1}
    assert parse_time_ms(doc, "date", "created_at", "timestamp") == 1_700_000_000_000


def test_parse_time_ms_skips_an_unparseable_string_for_the_next_key():
    doc = {"created_at": "not a date", "date": 1_700_000_000_000}
    assert parse_time_ms(doc, "created_at", "date") == 1_700_000_000_000


def test_parse_time_ms_falls_back_to_now():
    """A document with no timestamp is still worth storing — as "just now"."""
    before = int(time.time() * 1000)
    parsed = parse_time_ms({}, "date", "created_at")
    assert before <= parsed <= int(time.time() * 1000) + 1000


# ------------------------------------------------------- extract_iob_cob ----

def test_extract_iob_cob_reads_the_openaps_iob_block():
    assert extract_iob_cob({"openaps": {"iob": {"iob": 1.25}}}) == (1.25, None)


def test_extract_iob_cob_reads_suggested():
    doc = {"openaps": {"suggested": {"IOB": 2.0, "COB": 30}}}
    assert extract_iob_cob(doc) == (2.0, 30)


def test_extract_iob_cob_prefers_the_iob_block_over_suggested():
    doc = {"openaps": {"iob": {"iob": 1.0}, "suggested": {"IOB": 9.0, "COB": 5}}}
    assert extract_iob_cob(doc) == (1.0, 5)


def test_extract_iob_cob_falls_back_to_enacted():
    doc = {"openaps": {"enacted": {"IOB": 3.5, "COB": 12}}}
    assert extract_iob_cob(doc) == (3.5, 12)


def test_extract_iob_cob_reads_a_loop_document():
    doc = {"loop": {"iob": {"iob": 0.8}, "cob": {"cob": 14}}}
    assert extract_iob_cob(doc) == (0.8, 14)


@pytest.mark.parametrize("doc", [
    {},
    {"openaps": None},
    {"openaps": {"suggested": None}},
    {"loop": {"iob": "nonsense"}},
    {"openaps": {"iob": []}},
])
def test_extract_iob_cob_survives_junk(doc):
    assert extract_iob_cob(doc) == (None, None)


# --------------------------------------------------------------- writes ----

def test_add_entries_stores_and_returns_them(store):
    stored = store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    assert len(stored) == 1
    assert store.get_entries("Ada", 10)[0]["sgv"] == 120


def test_add_entries_accepts_the_glucose_alias(store):
    store.add_entries("Ada", [{"glucose": 99, "date": 1_700_000_000_000}])
    assert store.snapshot("Ada").sgv == 99


def test_add_entries_skips_documents_with_no_reading(store):
    stored = store.add_entries("Ada", [{"date": 1}, {"sgv": None}, "junk", None])
    assert stored == []
    assert store.get_entries("Ada", 10) == []


def test_entries_are_deduplicated_by_timestamp(store):
    """Pollers re-fetch the same window every minute; it must not pile up."""
    for _ in range(3):
        store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    assert len(store.get_entries("Ada", 10)) == 1


def test_a_re_sent_entry_replaces_the_earlier_value(store):
    store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    store.add_entries("Ada", [{"sgv": 125, "date": 1_700_000_000_000}])
    assert store.get_entries("Ada", 10)[0]["sgv"] == 125


def test_entries_are_kept_per_user(store):
    store.add_entries("Ada", [{"sgv": 120, "date": 1_700_000_000_000}])
    store.add_entries("Bo", [{"sgv": 200, "date": 1_700_000_000_000}])
    assert store.snapshot("Ada").sgv == 120
    assert store.snapshot("Bo").sgv == 200


def test_get_entries_returns_newest_first_and_honours_the_count(store):
    store.add_entries("Ada", [
        {"sgv": 100, "date": ago(15)},
        {"sgv": 110, "date": ago(10)},
        {"sgv": 120, "date": ago(5)},
    ])
    assert [e["sgv"] for e in store.get_entries("Ada", 2)] == [120, 110]


def test_add_treatments_generates_an_id_when_one_is_missing(store):
    stored = store.add_treatments("Ada", [{"eventType": "Bolus", "insulin": 2}])
    assert stored[0]["_id"]
    assert len(store.get_treatments("Ada", 10)) == 1


def test_treatments_are_upserted_on_their_id(store):
    """Nightscout sites re-send the same treatment on every poll."""
    for carbs in (20, 25):
        store.add_treatments("Ada", [{"_id": "abc", "eventType": "Carb Correction",
                                      "carbs": carbs, "created_at": ago(30)}])
    treatments = store.get_treatments("Ada", 10)
    assert len(treatments) == 1
    assert treatments[0]["carbs"] == 25


def test_delete_treatment_reports_whether_it_removed_anything(store):
    store.add_treatments("Ada", [{"_id": "abc", "insulin": 1}])
    assert store.delete_treatment("Ada", "abc") is True
    assert store.delete_treatment("Ada", "abc") is False
    assert store.get_treatments("Ada", 10) == []


def test_delete_treatment_will_not_reach_into_another_users_data(store):
    store.add_treatments("Bo", [{"_id": "abc", "insulin": 1}])
    assert store.delete_treatment("Ada", "abc") is False
    assert len(store.get_treatments("Bo", 10)) == 1


def test_add_devicestatus_extracts_iob_and_cob(store):
    store.add_devicestatus("Ada", [{
        "created_at": ago(2),
        "openaps": {"suggested": {"IOB": 1.5, "COB": 20}},
    }])
    snap = store.snapshot("Ada")
    assert (snap.iob, snap.cob) == (1.5, 20)


def test_devicestatus_is_deduplicated_by_timestamp(store):
    doc = {"created_at": 1_700_000_000_000, "openaps": {"iob": {"iob": 1}}}
    for _ in range(3):
        store.add_devicestatus("Ada", [doc])
    assert len(store.get_devicestatus("Ada", 10)) == 1


# --------------------------------------------------------------- params ----

def test_set_params_merges_and_never_clobbers_with_blanks(store):
    """Profile sources arrive piecemeal; a missing ISF must not erase one."""
    store.set_params("Ada", {"isf": 50, "cr": 10})
    store.set_params("Ada", {"isf": None, "dia_hours": 6})
    assert store.get_params("Ada") == {"isf": 50, "cr": 10, "dia_hours": 6}


def test_replace_params_drops_what_is_no_longer_there(store):
    store.replace_params("Ada", {"isf": 50, "error": "boom"})
    store.replace_params("Ada", {"isf": 50})
    assert store.get_params("Ada") == {"isf": 50}


def test_get_params_of_an_unknown_user_is_empty(store):
    assert store.get_params("Nobody") == {}


# ---------------------------------------------------------- rename_user ----

def test_rename_user_carries_history_over(store):
    store.add_entries("Old", [{"sgv": 120, "date": ago(5)}])
    store.add_treatments("Old", [{"_id": "t1", "insulin": 2, "created_at": ago(30)}])
    store.add_devicestatus("Old", [{"created_at": ago(3),
                                    "openaps": {"iob": {"iob": 1.0}}}])
    store.set_params("Old", {"isf": 45})

    store.rename_user("Old", "New")

    snap = store.snapshot("New")
    assert snap.sgv == 120
    assert snap.iob == 1.0
    assert snap.params == {"isf": 45}
    assert store.snapshot("Old").sgv is None


def test_rename_user_keeps_the_destinations_own_rows(store):
    """Renaming onto a name that already has data must not lose either side."""
    when = ago(5)
    store.add_entries("Old", [{"sgv": 100, "date": when}])
    store.add_entries("New", [{"sgv": 200, "date": when}])
    store.rename_user("Old", "New")
    assert store.snapshot("New").sgv == 200


@pytest.mark.parametrize("old, new", [("", "New"), ("Old", ""), ("Same", "Same")])
def test_rename_user_ignores_meaningless_renames(store, old, new):
    store.add_entries("Same", [{"sgv": 120, "date": ago(5)}])
    store.rename_user(old, new)
    assert store.snapshot("Same").sgv == 120


# ------------------------------------------------------------- snapshot ----

def test_snapshot_of_an_unknown_user_is_empty():
    assert UserSnapshot() == UserSnapshot(sgv=None)


def test_empty_snapshot_has_no_reading(store):
    snap = store.snapshot("Nobody")
    assert snap.sgv is None and snap.history == [] and snap.params == {}


def test_snapshot_reports_the_newest_reading_and_its_delta(store):
    store.add_entries("Ada", [
        {"sgv": 100, "date": ago(10)},
        {"sgv": 112, "date": ago(5), "direction": "FortyFiveUp"},
    ])
    snap = store.snapshot("Ada")
    assert snap.sgv == 112
    assert snap.direction == "FortyFiveUp"
    assert snap.delta == 12


def test_snapshot_withholds_a_delta_across_a_gap(store):
    """A 12 mg/dL step over an hour is not a trend worth showing."""
    store.add_entries("Ada", [
        {"sgv": 100, "date": ago(60)},
        {"sgv": 112, "date": ago(5)},
    ])
    assert store.snapshot("Ada").delta is None


def test_snapshot_history_is_the_requested_window_in_time_order(store):
    store.add_entries("Ada", [
        {"sgv": 90, "date": ago(400)},      # outside a 180-minute window
        {"sgv": 100, "date": ago(120)},
        {"sgv": 110, "date": ago(60)},
    ])
    history = store.snapshot("Ada", history_minutes=180).history
    assert [sgv for _ms, sgv in history] == [100, 110]


def test_snapshot_reports_the_last_carbs_and_bolus_separately(store):
    store.add_treatments("Ada", [
        {"_id": "c", "eventType": "Carb Correction", "carbs": 30,
         "created_at": ago(45)},
        {"_id": "b", "eventType": "Bolus", "insulin": 2.5, "created_at": ago(20)},
    ])
    snap = store.snapshot("Ada")
    assert snap.last_carbs == 30
    assert snap.last_bolus == 2.5
    assert snap.last_bolus_date > snap.last_carbs_date


def test_snapshot_boluses_cover_eight_hours_in_time_order(store):
    store.add_treatments("Ada", [
        {"_id": "old", "insulin": 1.0, "created_at": ago(9 * 60)},
        {"_id": "b1", "insulin": 2.0, "created_at": ago(200)},
        {"_id": "b2", "insulin": 3.0, "created_at": ago(30)},
    ])
    assert [u for _ms, u in store.snapshot("Ada").boluses] == [2.0, 3.0]


def test_snapshot_skips_a_devicestatus_carrying_neither_iob_nor_cob(store):
    """Trio uploads bare pump-battery statuses between the useful ones."""
    store.add_devicestatus("Ada", [
        {"created_at": ago(20), "openaps": {"suggested": {"IOB": 1.1}}},
        {"created_at": ago(1), "pump": {"battery": {"percent": 90}}},
    ])
    snap = store.snapshot("Ada")
    assert snap.iob == 1.1


def test_snapshot_keeps_the_raw_status_document(store):
    """The forecast curve lives in there."""
    doc = {"created_at": ago(2),
           "openaps": {"suggested": {"IOB": 1.0, "predBGs": {"IOB": [120, 118]}}}}
    store.add_devicestatus("Ada", [doc])
    snap = store.snapshot("Ada")
    assert snap.status_raw["openaps"]["suggested"]["predBGs"]["IOB"] == [120, 118]


def test_snapshot_ignores_a_zero_bolus(store):
    store.add_treatments("Ada", [{"_id": "b", "insulin": 0, "created_at": ago(5)}])
    assert store.snapshot("Ada").last_bolus is None


# ---------------------------------------------------------- concurrency ----

def test_concurrent_writers_and_readers_do_not_corrupt_the_store(tmp_path):
    """One store, several server threads plus the display loop reading."""
    db = Store(str(tmp_path / "concurrent.db"))
    errors: list[BaseException] = []

    def writer(user: str, offset: int):
        try:
            for i in range(40):
                db.add_entries(user, [{"sgv": 100 + i, "date": ago(200 - i) + offset}])
        except BaseException as exc:  # noqa: BLE001 - reported at the end
            errors.append(exc)

    def reader():
        try:
            for _ in range(40):
                db.snapshot("Ada")
                db.get_entries("Bo", 10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=("Ada", 0)),
               threading.Thread(target=writer, args=("Bo", 1)),
               threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(db.get_entries("Ada", 100)) == 40
    assert len(db.get_entries("Bo", 100)) == 40
    db.close()


def test_a_store_survives_reopening_the_same_file(tmp_path):
    path = str(tmp_path / "glucocube.db")
    first = Store(path)
    first.add_entries("Ada", [{"sgv": 120, "date": ago(5)}])
    first.close()

    second = Store(path)
    assert second.snapshot("Ada").sgv == 120
    second.close()


def test_raw_documents_are_stored_verbatim(store):
    doc = {"sgv": 120, "date": ago(5), "device": "trio", "extra": {"a": [1, 2]}}
    store.add_entries("Ada", [doc])
    assert json.loads(json.dumps(store.get_entries("Ada", 1)[0])) == doc
