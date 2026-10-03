"""Tree persistence: S3 in Lambda, a local directory for dev and tests.

Layout under the tree's root (``s3://$TREE_BUCKET/trees/<name>/`` or
``$TREE_DIR/<name>/``)::

    tree.json        canonical model (source of truth)
    tree.ged         latest GEDCOM export
    resources/...    attached documents, images, record scans
"""

import os
import re
from functools import cache
from pathlib import Path
from typing import Protocol

from genealogy._models import Tree

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
URL_EXPIRES = 3600  # seconds a file_url link stays valid


class ConcurrentModification(RuntimeError):
    pass


def _check_name(tree_name: str) -> str:
    if not _SAFE_NAME.match(tree_name):
        raise ValueError(
            f"Invalid tree name {tree_name!r}: use letters, digits, '.', '_', '-'"
        )
    return tree_name


class TreeStore(Protocol):
    def load(self, tree_name: str) -> tuple[Tree, str | None]:
        """Return the tree and an opaque version token (None if new)."""
        ...

    def save(self, tree: Tree, version: str | None) -> None:
        """Save; raise ConcurrentModification if the stored version moved."""
        ...

    def put_file(
        self, tree_name: str, rel_path: str, data: bytes, content_type: str
    ) -> str:
        """Write a file under the tree root; return a locator (s3:// URI or path)."""
        ...

    def read_file(self, tree_name: str, rel_path: str) -> bytes: ...

    def copy_file(self, tree_name: str, src: str, dest: str) -> None: ...

    def list_files(self, tree_name: str, prefix: str = "") -> list[dict]: ...

    def file_url(self, tree_name: str, rel_path: str) -> str: ...

    def list_trees(self) -> list[str]: ...


class LocalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _dir(self, tree_name: str) -> Path:
        return self.root / _check_name(tree_name)

    def load(self, tree_name: str) -> tuple[Tree, str | None]:
        path = self._dir(tree_name) / "tree.json"
        if not path.exists():
            return Tree(name=tree_name), None
        raw = path.read_text()
        return Tree.model_validate_json(raw), str(path.stat().st_mtime_ns)

    def save(self, tree: Tree, version: str | None) -> None:
        path = self._dir(tree.name) / "tree.json"
        current = str(path.stat().st_mtime_ns) if path.exists() else None
        if current != version:
            raise ConcurrentModification(
                f"tree {tree.name!r} changed since it was loaded"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tree.model_dump_json(indent=2))

    def put_file(
        self, tree_name: str, rel_path: str, data: bytes, content_type: str
    ) -> str:
        path = self._dir(tree_name) / _safe_rel(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def read_file(self, tree_name: str, rel_path: str) -> bytes:
        path = self._dir(tree_name) / _safe_rel(rel_path)
        if not path.is_file():
            raise ValueError(f"No stored file {rel_path!r} in tree {tree_name!r}")
        return path.read_bytes()

    def copy_file(self, tree_name: str, src: str, dest: str) -> None:
        self.put_file(tree_name, dest, self.read_file(tree_name, src), "")

    def list_files(self, tree_name: str, prefix: str = "") -> list[dict]:
        base = self._dir(tree_name)
        if not base.exists():
            return []
        return [
            {"path": str(p.relative_to(base)), "size": p.stat().st_size}
            for p in sorted(base.rglob("*"))
            if p.is_file() and str(p.relative_to(base)).startswith(prefix)
        ]

    def file_url(self, tree_name: str, rel_path: str) -> str:
        return (self._dir(tree_name) / _safe_rel(rel_path)).resolve().as_uri()

    def list_trees(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "tree.json").exists())


class S3Store:
    def __init__(self, bucket: str, prefix: str = "trees/", client=None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/"
        self.s3 = client or boto3.client("s3")

    def _key(self, tree_name: str, rel_path: str) -> str:
        return f"{self.prefix}{_check_name(tree_name)}/{_safe_rel(rel_path)}"

    def load(self, tree_name: str) -> tuple[Tree, str | None]:
        try:
            obj = self.s3.get_object(
                Bucket=self.bucket, Key=self._key(tree_name, "tree.json")
            )
        except self.s3.exceptions.NoSuchKey:
            return Tree(name=tree_name), None
        return Tree.model_validate_json(obj["Body"].read()), obj["ETag"]

    def save(self, tree: Tree, version: str | None) -> None:
        # Conditional write so two overlapping Lambda invocations can't silently
        # clobber each other: If-Match on update, If-None-Match on create.
        cond = {"IfMatch": version} if version else {"IfNoneMatch": "*"}
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self._key(tree.name, "tree.json"),
                Body=tree.model_dump_json(indent=2).encode(),
                ContentType="application/json",
                **cond,
            )
        except self.s3.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise ConcurrentModification(
                    f"tree {tree.name!r} changed since it was loaded"
                ) from exc
            raise

    def put_file(
        self, tree_name: str, rel_path: str, data: bytes, content_type: str
    ) -> str:
        key = self._key(tree_name, rel_path)
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return f"s3://{self.bucket}/{key}"

    def read_file(self, tree_name: str, rel_path: str) -> bytes:
        try:
            obj = self.s3.get_object(
                Bucket=self.bucket, Key=self._key(tree_name, rel_path)
            )
        except self.s3.exceptions.NoSuchKey:
            raise ValueError(
                f"No stored file {rel_path!r} in tree {tree_name!r}"
            ) from None
        return obj["Body"].read()

    def copy_file(self, tree_name: str, src: str, dest: str) -> None:
        # Server-side copy: no re-upload of the bytes.
        self.s3.copy_object(
            Bucket=self.bucket,
            Key=self._key(tree_name, dest),
            CopySource={"Bucket": self.bucket, "Key": self._key(tree_name, src)},
        )

    def list_files(self, tree_name: str, prefix: str = "") -> list[dict]:
        base = f"{self.prefix}{_check_name(tree_name)}/"
        files = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=base + prefix
        ):
            files += [
                {"path": o["Key"][len(base) :], "size": o["Size"]}
                for o in page.get("Contents", [])
            ]
        return files

    def file_url(self, tree_name: str, rel_path: str) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(tree_name, rel_path)},
            ExpiresIn=URL_EXPIRES,
        )

    def list_trees(self) -> list[str]:
        resp = self.s3.list_objects_v2(
            Bucket=self.bucket, Prefix=self.prefix, Delimiter="/"
        )
        return sorted(
            p["Prefix"][len(self.prefix) :].rstrip("/")
            for p in resp.get("CommonPrefixes", [])
        )


def _safe_rel(rel_path: str) -> str:
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"Invalid path {rel_path!r}")
    return "/".join(parts)


def get_store() -> TreeStore:
    """The store for the current environment, reused across warm invocations."""
    return _store(
        os.environ.get("TREE_BUCKET"),
        os.environ.get("TREE_PREFIX", "trees/"),
        os.environ.get("TREE_DIR", str(Path.home() / ".genealogy-mcp" / "trees")),
    )


@cache
def _store(bucket: str | None, prefix: str, local_dir: str) -> TreeStore:
    return S3Store(bucket, prefix) if bucket else LocalStore(local_dir)
