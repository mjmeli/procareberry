"""In-memory Firestore transport; actual pinned library logging code runs unchanged."""

import copy

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1 import DELETE_FIELD


class Version:
    def __init__(self, value):
        self.value = value

    def rfc3339(self):
        return str(self.value)

    def __eq__(self, other):
        return isinstance(other, Version) and self.value == other.value


class Snapshot:
    def __init__(self, ref):
        self.reference, self.id = ref, ref.path.split("/")[-1]
        self.exists = ref.path in ref.db.data
        self.raw = copy.deepcopy(ref.db.data.get(ref.path))
        self.update_time = Version(ref.db.versions.get(ref.path, 0))

    def to_dict(self):
        return copy.deepcopy(self.raw)


def merge(target, incoming):
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def collection(self, name):
        return Collection(self.db, self.path + "/" + name)

    async def get(self, **kwargs):
        if self.db.fail_reads:
            raise RuntimeError("synthetic read failure")
        return Snapshot(self)

    async def set(self, payload, merge=False):
        self.db.write(self.path, payload, "merge" if merge else "set")

    async def update(self, payload, option=None):
        self.db.validate(self.path, "update", option)
        self.db.write(self.path, payload, "update")


class Collection:
    def __init__(self, db, path, filters=()):
        self.db, self.path, self.filters = db, path, filters

    def document(self, name):
        return Ref(self.db, self.path + "/" + name)

    def where(self, *, filter):
        return Collection(self.db, self.path, self.filters + (filter,))

    def order_by(self, *args):
        return self

    async def stream(self):
        if self.db.fail_reads:
            raise RuntimeError("synthetic stream failure")
        for path, payload in list(self.db.data.items()):
            if path.rsplit("/", 1)[0] != self.path:
                continue
            match = True
            for f in self.filters:
                value = payload.get(f.field_path)
                match &= (
                    value is not None
                    and {
                        "==": lambda: value == f.value,
                        "<": lambda: value < f.value,
                        ">=": lambda: value >= f.value,
                    }[f.op_string]()
                )
            if match:
                yield Snapshot(Ref(self.db, path))


class Batch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def create(self, ref, payload):
        self.ops.append((ref.path, payload, "create", None))

    def update(self, ref, payload, option=None):
        self.ops.append((ref.path, payload, "update", option))

    async def commit(self):
        for path, _, operation, option in self.ops:
            self.db.validate(path, operation, option)
        for path, payload, operation, _ in self.ops:
            self.db.write(path, payload, "set" if operation == "create" else operation)
        return []


class DB:
    def __init__(self):
        self.data, self.versions = {}, {}
        self.fail_reads = False
        self.write_count = 0

    def collection(self, name):
        return Collection(self, name)

    def document(self, path):
        return Ref(self, path)

    def batch(self):
        return Batch(self)

    def validate(self, path, operation, option):
        if operation == "create" and path in self.data:
            raise AlreadyExists("already exists")
        if operation == "update" and path not in self.data:
            raise NotFound("missing")
        if option and option._last_update_time != Version(self.versions.get(path, 0)):
            raise FailedPrecondition("version changed")

    def write(self, path, payload, operation="set"):
        self.write_count += 1
        if operation == "set":
            self.data[path] = copy.deepcopy(payload)
        elif operation == "merge":
            merge(self.data.setdefault(path, {}), payload)
        elif operation == "update":
            for key, value in payload.items():
                parts = key.split(".")
                target = self.data[path]
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                if value is DELETE_FIELD:
                    target.pop(parts[-1], None)
                else:
                    target[parts[-1]] = copy.deepcopy(value)
        self.versions[path] = self.versions.get(path, 0) + 1

    async def close(self):
        pass
