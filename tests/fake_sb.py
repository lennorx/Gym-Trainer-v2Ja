"""Supabase simulat în memorie (suficient pentru app.py din GymBro)."""
import copy, uuid

DB = {"gym_sessions": [], "gym_history": [], "gym_users": [], "gym_tokens": [], "gym_password_resets": []}
_hid = [0]


class Resp:
    def __init__(self, data):
        self.data = data


class Q:
    def __init__(self, table):
        assert table in DB, f"tabel necunoscut: {table}"
        self.t, self.filters, self.op, self.payload = table, [], "select", None
        self._order, self._limit, self._ignore = None, None, False

    def select(self, cols="*", count=None): self.op = "select"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def upsert(self, row, ignore_duplicates=False): self.op, self.payload, self._ignore = "upsert", row, ignore_duplicates; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.filters.append(lambda r: r.get(c) == v); return self
    def in_(self, c, vs): vs = list(vs); self.filters.append(lambda r: r.get(c) in vs); return self
    def is_(self, c, v): self.filters.append(lambda r: r.get(c) is None); return self
    def order(self, c, desc=False): self._order = (c, desc); return self
    def limit(self, n): self._limit = n; return self

    def execute(self):
        rows = DB[self.t]
        match = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "select":
            if self._order:
                match = sorted(match, key=lambda r: r.get(self._order[0]) or 0, reverse=self._order[1])
            if self._limit is not None:
                match = match[: self._limit]
            return Resp(copy.deepcopy(match))
        if self.op in ("insert", "upsert"):
            row = dict(self.payload)
            if self.t == "gym_users":
                row.setdefault("id", str(uuid.uuid4()))
                if any(r["email"] == row["email"] for r in rows):
                    raise Exception("duplicate key value violates unique constraint")
            if self.t == "gym_history":
                if not any(s["session_id"] == row["session_id"] for s in DB["gym_sessions"]):
                    raise Exception("violates foreign key constraint gym_history_session_id_fkey")
                _hid[0] += 1; row["id"] = _hid[0]
            if self.t == "gym_sessions":
                ex = [r for r in rows if r["session_id"] == row["session_id"]]
                if ex:
                    if self.op == "upsert" and self._ignore:
                        return Resp([])
                    if self.op == "upsert":
                        ex[0].update(row); return Resp([ex[0]])
                    raise Exception("duplicate key")
                row.setdefault("title", None)
            if self.t == "gym_password_resets" and self.op == "upsert":
                DB[self.t] = [r for r in rows if r["user_id"] != row["user_id"]]; rows = DB[self.t]
            rows.append(row)
            return Resp([copy.deepcopy(row)])
        if self.op == "update":
            for r in match: r.update(self.payload)
            return Resp(copy.deepcopy(match))
        if self.op == "delete":
            if self.t == "gym_sessions":
                gone = {r["session_id"] for r in match}
                DB["gym_history"] = [h for h in DB["gym_history"] if h["session_id"] not in gone]
            DB[self.t] = [r for r in rows if r not in match]
            return Resp(copy.deepcopy(match))


class FakeClient:
    def table(self, name): return Q(name)


def create_client(url, key): return FakeClient()
