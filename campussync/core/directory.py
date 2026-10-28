"""Who is who, between real Gmail addresses and the (mock) VTOP directory.

- The owner is whoever connected their Gmail. They pick which VTOP profile holds their timetable
  and the name replies are signed with.
- A real address can be linked to a VTOP profile (a teammate's Gmail -> student S002). The agent then
  checks that profile's timetable when that person mails.
- Anyone else who mails becomes an outside contact (id X0001...): no timetable, so the agent only
  checks the owner's side.

Stored in the state file's settings block, so it survives a demo reset."""
from __future__ import annotations

import re
from dataclasses import replace

from ..models import Person

MOCK_DOMAIN = re.compile(r"\.mock$", re.I)


def is_real_address(addr: str | None) -> bool:
    return bool(addr) and "@" in addr and not MOCK_DOMAIN.search(addr.strip())


class Directory:
    def __init__(self, store, base_portal):
        self.store, self.base = store, base_portal

    # ---------------------------------------------------------------- storage
    @property
    def _data(self) -> dict:
        d = self.store.data["settings"].setdefault("gmail", {})
        d.setdefault("contacts", {})
        d.setdefault("owner", None)
        d.setdefault("counter", 0)
        return d

    def _save(self) -> None:
        self.store.save()

    # ---------------------------------------------------------------- owner
    @property
    def owner(self) -> dict | None:
        return self._data["owner"]

    @property
    def owner_id(self) -> str | None:
        return self.owner["pid"] if self.owner else None

    @property
    def owner_email(self) -> str | None:
        return self.owner["email"] if self.owner else None

    def set_owner(self, email: str, pid: str, name: str = "") -> None:
        self._data["owner"] = {"email": email.lower().strip(), "pid": pid, "name": name.strip()}
        self._data["contacts"].pop(email.lower().strip(), None)
        self._save()

    def clear_owner(self) -> None:
        self._data["owner"] = None
        self._save()

    # ---------------------------------------------------------------- contacts
    def contacts(self) -> list[dict]:
        return [{"email": e, **c} for e, c in sorted(self._data["contacts"].items())]

    def contact_by_xid(self, xid: str) -> tuple[str, dict] | None:
        for e, c in self._data["contacts"].items():
            if c.get("xid") == xid:
                return e, c
        return None

    def real_email(self, pid: str) -> str | None:
        """The real address mail to this person should go to, if there is one."""
        if self.owner and pid == self.owner["pid"]:
            return self.owner["email"]
        for e, c in self._data["contacts"].items():
            if c.get("pid") == pid or c.get("xid") == pid:
                return e
        return None

    def resolve(self, email: str, name: str = "") -> str:
        """Address -> person id. Creates an outside contact for unknown addresses."""
        e = email.lower().strip()
        if self.owner and e == self.owner["email"]:
            return self.owner["pid"]
        c = self._data["contacts"].get(e)
        if c:
            if name and not c.get("name"):
                c["name"] = name
                self._save()
            return c.get("pid") or c["xid"]
        for p in self.base.list_people():
            if p.email and p.email.lower() == e:
                return p.id
        self._data["counter"] += 1
        xid = f"X{self._data['counter']:04d}"
        self._data["contacts"][e] = {"xid": xid, "name": name or e.split("@")[0], "pid": None}
        self._save()
        return xid

    def link(self, email: str, pid: str | None) -> tuple[str | None, str]:
        """Link a real address to a VTOP profile (or unlink with None). Returns (old id, new id)."""
        e = email.lower().strip()
        old = self.resolve(e)
        c = self._data["contacts"][e] if e in self._data["contacts"] else None
        if c is None:
            raise ValueError("That's your own address")
        for other, oc in self._data["contacts"].items():  # a profile has one real address
            if pid and other != e and oc.get("pid") == pid:
                oc["pid"] = None
        c["pid"] = pid
        self._save()
        return old, (pid or c["xid"])

    # ---------------------------------------------------------------- people
    def external_person(self, xid: str) -> Person | None:
        hit = self.contact_by_xid(xid)
        if not hit:
            return None
        e, c = hit
        return Person(id=xid, name=c.get("name") or e, email=e, role="external", department="Outside VTOP")

    def decorate(self, p: Person | None) -> Person | None:
        """Show the real name/address on profiles that are linked to a real person."""
        if p is None:
            return None
        if self.owner and p.id == self.owner["pid"]:
            return replace(p, email=self.owner["email"], name=self.owner.get("name") or p.name)
        for e, c in self._data["contacts"].items():
            if c.get("pid") == p.id:
                return replace(p, email=e)
        return p
