"""Verified atomic writes for the local filesystem object store."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int


class StorageBackend(Protocol):
    def exists(self, key: str) -> bool: ...

    def open_read(self, key: str) -> BinaryIO: ...

    def write_from_path_atomic(self, key: str, source: Path, sha256: str) -> StoredObject: ...


class LocalFsBackend:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        parts = key.split("/")
        if not key or any(p in {"", ".", ".."} for p in parts) or "\\" in key or ":" in key:
            raise ValueError("Storage keys must be relative POSIX paths without traversal")
        target = self.root.joinpath(*parts).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Storage key escapes the data root")
        return target

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def open_read(self, key: str) -> BinaryIO:
        return self.path(key).open("rb")

    def verify(self, obj: StoredObject) -> None:
        path = self.path(obj.key)
        if not path.is_file() or path.stat().st_size != obj.size_bytes:
            raise ValueError(f"Missing or truncated object: {obj.key}")
        if sha256_file(path) != obj.sha256:
            raise ValueError(f"Checksum mismatch: {obj.key}")

    def write_from_path_atomic(self, key: str, source: Path, sha256: str) -> StoredObject:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".geoml-", suffix=".part", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
                output.flush()
                os.fsync(output.fileno())
            if sha256_file(temporary) != sha256:
                raise ValueError(f"Checksum mismatch: {key}")
            if target.exists():
                if sha256_file(target) != sha256:
                    raise ValueError(f"Existing object has different content: {key}")
            else:
                os.replace(temporary, target)
            return StoredObject(key, sha256, target.stat().st_size)
        finally:
            temporary.unlink(missing_ok=True)

    def write_bytes(self, key: str, content: bytes) -> StoredObject:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(content)
            return self.write_from_path_atomic(key, source, hashlib.sha256(content).hexdigest())
