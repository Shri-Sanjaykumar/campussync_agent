"""The agent's own small database (JSON file). It holds what the agent owns:
uploaded commitments, booked meetings, reminders, alerts, pending offers.
The mock mail server and mock calendar also keep their data here."""
from __future__ import annotations

import copy
import itertools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import Alert, Commitment, Mail, Meeting, Reminder, from_dict, to_dict

COLLECTIONS = ["commitments", "meetings", "mails", "reminders", "alerts", "offers", "calendar", "holds",
               "waitlist", "notifications", "prefs", "imported_timetables", "leave_requests", "deadlines", "triage"]
DICT_COLLECTIONS = {"calendar", "prefs", "imported_timetables", "triage"}
DEFAULT_PREFS = {"earliest": None, "latest": None, "max_per_day": None, "gap_min": 0, "no_meeting_days": [],
                 "telegram_chat_id": "", "notify_telegram": False}


# One lock per state file for the whole process. Streamlit gives every browser tab (and the background
# Gmail check) its own StateStore on its own thread, so they must take turns on the same file.
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()
_TMP_COUNTER = itertools.count()
# Windows refuses to replace or read a file for a moment while another handle (another tab,
# antivirus, OneDrive, search indexer) has it open, so those calls are retried.
_RETRY_DELAYS = [0.02, 0.05, 0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.5, 0.5, 0.8, 1.0]


def _file_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(Path(path).resolve()))
    with _FILE_LOCKS_GUARD:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.RLock()
        return _FILE_LOCKS[key]


def _with_retries(fn):
    for delay in _RETRY_DELAYS:
        try:
            return fn()
        except PermissionError:
            time.sleep(delay)
    return fn()


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = _file_lock(self.path)
        self.data: dict[str, Any] = {}
        self._index: dict[str, Any] = {}
        self._index_version = None
        self.epoch = 0  # bumps on every load/reset so caches never mix old and new data
        self.load()

    # ------------------------------------------------------------ basics
    def load(self) -> None:
        with self._lock:
            self.data = self._read()
            self._index_version = None
            self.epoch += 1
            self._init_defaults()

    def _init_defaults(self) -> None:
        with self._lock:
            for c in COLLECTIONS:
                self.data.setdefault(c, {} if c in DICT_COLLECTIONS else [])
            self.data.setdefault("counters", {})
            self.data.setdefault("settings", {})
            self.data.setdefault("version", 0)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        for _ in range(5):
            try:
                text = _with_retries(lambda: self.path.read_text(encoding="utf-8"))
                return json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                time.sleep(0.1)  # caught a half-written file from another program; read again
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        with self._lock:
            self.data["version"] = self.data.get("version", 0) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(self.data, indent=1, ensure_ascii=False)
            # a fresh temp name per save, so two writers never fight over the same temp file
            tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.{threading.get_ident()}.{next(_TMP_COUNTER)}.tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                _with_retries(lambda: os.replace(tmp, self.path))
            except PermissionError:
                # still locked after ~4 s: write in place (not atomic, but the save isn't lost)
                _with_retries(lambda: self.path.write_text(text, encoding="utf-8"))
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def reset(self) -> None:
        with self._lock:
            self.data = {}
            self._index_version = None
            self.epoch += 1
            self._init_defaults()
            self.save()

    @property
    def version(self) -> int:
        return self.data.get("version", 0)

    def new_id(self, prefix: str) -> str:
        n = self.data["counters"].get(prefix, 0) + 1
        self.data["counters"][prefix] = n
        return f"{prefix}{n:04d}"

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.data["settings"].get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.data["settings"][key] = value
        self.save()

    # ------------------------------------------------------------ indexes (rebuilt once per saved version)
    def _idx(self) -> dict:
        """Per-person indexes over the raw records, so lookups don't scan and parse everything."""
        if self._index_version != (self.epoch, self.version):
            by_meet, by_comm = {}, {}
            for d in self.data["meetings"]:
                for pid in d.get("participant_ids", []):
                    by_meet.setdefault(pid, []).append(d)
            for d in self.data["commitments"]:
                by_comm.setdefault(d["person_id"], []).append(d)
            self._index = {"meetings": by_meet, "commitments": by_comm, "parsed": {}}
            self._index_version = (self.epoch, self.version)
        return self._index

    def _parsed(self, key: tuple, raw: list[dict], cls: type) -> list:
        cache = self._idx()["parsed"]
        if key not in cache:
            cache[key] = [from_dict(cls, d) for d in raw]
        return [copy.deepcopy(x) for x in cache[key]]

    # ------------------------------------------------------------ generic typed helpers
    def _all(self, coll: str, cls: type) -> list:
        return [from_dict(cls, d) for d in self.data[coll]]

    def _upsert(self, coll: str, obj: Any) -> None:
        d = to_dict(obj)
        items = self.data[coll]
        for i, x in enumerate(items):
            if x["id"] == d["id"]:
                items[i] = d
                break
        else:
            items.append(d)
        self.save()

    def _delete(self, coll: str, pred: Callable[[dict], bool]) -> int:
        before = len(self.data[coll])
        self.data[coll] = [x for x in self.data[coll] if not pred(x)]
        n = before - len(self.data[coll])
        if n:
            self.save()
        return n

    # ------------------------------------------------------------ commitments
    def commitments(self, person_id: str | None = None) -> list[Commitment]:
        if person_id is None:
            return self._all("commitments", Commitment)
        return self._parsed(("c", person_id), self._idx()["commitments"].get(person_id, []), Commitment)

    def upsert_commitment(self, c: Commitment) -> None:
        self._upsert("commitments", c)

    def delete_commitment(self, cid: str) -> int:
        return self._delete("commitments", lambda x: x["id"] == cid)

    # ------------------------------------------------------------ meetings
    def meetings(self, person_id: str | None = None, include_cancelled: bool = False) -> list[Meeting]:
        if person_id is None:
            items = self._parsed(("m", "*"), self.data["meetings"], Meeting)
        else:
            items = self._parsed(("m", person_id), self._idx()["meetings"].get(person_id, []), Meeting)
        out = []
        for m in items:
            if not include_cancelled and m.status == "cancelled":
                continue
            if person_id and person_id not in m.participant_ids:
                continue
            out.append(m)
        return sorted(out, key=lambda m: m.start)

    def get_meeting(self, mid: str) -> Meeting | None:
        for d in self.data["meetings"]:
            if d["id"] == mid:
                return from_dict(Meeting, d)
        return None

    def upsert_meeting(self, m: Meeting) -> None:
        self._upsert("meetings", m)

    # ------------------------------------------------------------ mails (used by the mock mail adapter)
    def mails(self) -> list[Mail]:
        return self._all("mails", Mail)

    def get_mail(self, mail_id: str) -> Mail | None:
        for d in self.data["mails"]:
            if d["id"] == mail_id:
                return from_dict(Mail, d)
        return None

    def upsert_mail(self, m: Mail) -> None:
        self._upsert("mails", m)

    # ------------------------------------------------------------ reminders
    def reminders(self) -> list[Reminder]:
        return self._all("reminders", Reminder)

    def upsert_reminder(self, r: Reminder) -> None:
        self._upsert("reminders", r)

    # ------------------------------------------------------------ alerts
    def alerts(self, person_id: str | None = None) -> list[Alert]:
        items = self._all("alerts", Alert)
        return sorted([a for a in items if person_id is None or a.person_id == person_id],
                      key=lambda a: a.created_at, reverse=True)

    def add_alert(self, a: Alert) -> None:
        self._upsert("alerts", a)

    # ------------------------------------------------------------ offers (options sent, waiting for a pick)
    def offers(self) -> list[dict]:
        return list(self.data["offers"])

    def add_offer(self, offer: dict) -> None:
        replaced = [o["id"] for o in self.data["offers"] if o["thread_id"] == offer["thread_id"]
                    and o["offered_to"] == offer["offered_to"]]
        self.data["offers"] = [o for o in self.data["offers"] if o["id"] not in replaced]
        for h in self.data["holds"]:
            if h["offer_id"] in replaced and h["status"] == "active":
                h["status"] = "released"
        self.data["offers"].append(offer)
        self.save()

    def close_offer(self, offer_id: str) -> None:
        for o in self.data["offers"]:
            if o["id"] == offer_id:
                o["status"] = "closed"
        self.save()

    # ------------------------------------------------------------ holds (offered options kept free for a while)
    def add_holds(self, holds: list[dict]) -> None:
        self.data["holds"].extend(holds)
        self.save()

    def active_holds(self, person_id: str, now) -> list[dict]:
        out = []
        for h in self.data["holds"]:
            if h["status"] == "active" and person_id in h["participants"] and h["expires_at"] > now.isoformat(timespec="minutes"):
                out.append(h)
        return out

    def release_holds(self, offer_id: str) -> None:
        changed = False
        for h in self.data["holds"]:
            if h["offer_id"] == offer_id and h["status"] == "active":
                h["status"] = "released"
                changed = True
        if changed:
            self.save()

    # ------------------------------------------------------------ preferences
    def prefs(self, person_id: str) -> dict:
        return {**DEFAULT_PREFS, **self.data["prefs"].get(person_id, {})}

    def set_prefs(self, person_id: str, prefs: dict) -> None:
        self.data["prefs"][person_id] = {**self.prefs(person_id), **prefs}
        self.save()

    # ------------------------------------------------------------ office-hours waitlist
    def waitlist(self) -> list[dict]:
        return list(self.data["waitlist"])

    def add_waitlist(self, entry: dict) -> None:
        self.data["waitlist"].append(entry)
        self.save()

    def update_waitlist(self, entry_id: str, **changes) -> None:
        for w in self.data["waitlist"]:
            if w["id"] == entry_id:
                w.update(changes)
        self.save()

    # ------------------------------------------------------------ notification log (Telegram etc.)
    def log_notification(self, entry: dict) -> None:
        self.data["notifications"].append(entry)
        self.data["notifications"] = self.data["notifications"][-500:]
        self.save()

    # ------------------------------------------------------------ imported timetables
    def imported_timetable(self, person_id: str) -> list[dict] | None:
        return self.data["imported_timetables"].get(person_id)

    def set_imported_timetable(self, person_id: str, rows: list[dict] | None) -> None:
        if rows is None:
            self.data["imported_timetables"].pop(person_id, None)
        else:
            self.data["imported_timetables"][person_id] = rows
        self.save()
