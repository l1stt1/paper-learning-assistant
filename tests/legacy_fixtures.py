"""Existing-record fixtures for retired note/annotation editors; never production data."""
import json
import uuid


def seed_note(lib, aid, note):
    with lib.db(aid) as db:
        db.execute("INSERT INTO notes(created_at,body) VALUES(?,?)", ("2026-01-01T00:00:00+00:00", note))


def seed_annotation(lib, aid, payload, archived=False):
    record = {**payload, "id": uuid.uuid4().hex, "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"}
    record.pop("article_id", None)
    record.pop("action", None)
    with lib.db(aid) as db:
        db.execute("INSERT INTO annotations VALUES(?,?,?,?,?)", (record["id"], record["page"], record["created_at"], json.dumps(record, ensure_ascii=False), int(archived)))
    return record
