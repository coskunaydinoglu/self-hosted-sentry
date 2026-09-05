from app.missing import collect_missing, sort_entries

DSYM_ID = "A1B2C3D4-0000-1111-2222-333344445555"


def event(event_id, date, errors, release="1.2.0"):
    return {
        "eventID": event_id,
        "dateCreated": date,
        "release": {"version": release} if release else None,
        "errors": errors,
    }


def test_collect_aggregates_by_debug_id():
    events = [
        event("e1", "2026-09-01T10:00:00Z", [
            {"type": "native_missing_dsym", "data": {"image_uuid": DSYM_ID, "image_path": "/private/var/MyApp"}},
            {"type": "native_missing_optionally_bundled_dsym", "data": {"image_uuid": "0000-sys", "image_path": "/usr/lib/libsystem_c.dylib"}},
        ]),
        event("e2", "2026-09-02T10:00:00Z", [
            {"type": "native_missing_dsym", "data": {"image_uuid": DSYM_ID.lower(), "image_path": "/private/var/MyApp"}},
            {"type": "native_missing_dsym", "data": {"image_uuid": DSYM_ID}},  # duplicate within one event
        ], release="1.3.0"),
        event("e3", "2026-09-03T10:00:00Z", [
            {"type": "proguard_missing_mapping", "data": {"mapping_uuid": "9f0e-pg"}},
            {"type": "invalid_data", "data": {"name": "foo"}},
        ], release=None),
        event("e4", "2026-09-03T11:00:00Z", []),
    ]
    entries = collect_missing(events)
    assert set(entries) == {DSYM_ID.lower(), "0000-sys", "9f0e-pg"}

    app = entries[DSYM_ID.lower()]
    assert app.kind == "required"
    assert app.event_count == 2
    assert app.image_path == "/private/var/MyApp"
    assert app.releases == {"1.2.0", "1.3.0"}
    assert app.first_seen == "2026-09-01T10:00:00Z" and app.last_seen == "2026-09-02T10:00:00Z"
    assert app.sample_events == ["e1", "e2"]
    assert entries["0000-sys"].kind == "optional"
    assert entries["9f0e-pg"].kind == "proguard"

    ordered = sort_entries(entries)
    assert [e.kind for e in ordered] == ["required", "proguard", "optional"]
    assert ordered[0].to_dict()["releases"] == ["1.2.0", "1.3.0"]
