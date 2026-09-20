"""Inspect and synchronize Zotero Desktop through its official Local API.

Examples::

    python -m app.cli.zotero_local probe --json
    python -m app.cli.zotero_local collections --json
    python -m app.cli.zotero_local sync --library-id <uuid> --json
    python -m app.cli.zotero_local status --library-id <uuid> --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from app.core.db import dispose_engine, get_sessionmaker
from app.schemas.zotero_local import ZoteroBindingRead, ZoteroSyncRunRead
from app.services import zotero_local


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "collections"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true", dest="as_json")
    sync = commands.add_parser("sync")
    sync.add_argument("--library-id", type=uuid.UUID, required=True)
    sync.add_argument("--full", action="store_true")
    sync.add_argument("--json", action="store_true", dest="as_json")
    status = commands.add_parser("status")
    status.add_argument("--library-id", type=uuid.UUID, required=True)
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _json_default(value: object) -> str:
    if isinstance(value, (uuid.UUID, datetime, date)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return str(value)


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default))
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print("\t".join(str(item.get(key, "")) for key in ("key", "name", "parent_key")))
            else:
                print(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


async def _run(args: argparse.Namespace) -> None:
    try:
        if args.command in {"probe", "collections"}:
            async with zotero_local.ZoteroLocalClient() as client:
                if args.command == "probe":
                    _emit(asdict(await client.probe()), as_json=args.as_json)
                else:
                    _emit(
                        [asdict(item) for item in await client.collections()],
                        as_json=args.as_json,
                    )
            return

        async with get_sessionmaker()() as session:
            binding = await zotero_local.get_binding(session, library_id=args.library_id)
            if binding is None:
                raise zotero_local.ZoteroLocalError("ZOTERO_BINDING_NOT_FOUND")
            if args.command == "status":
                run = await zotero_local.latest_sync_run(session, binding_id=binding.id)
                payload = {
                    "binding": ZoteroBindingRead.model_validate(binding).model_dump(mode="json"),
                    "run": (
                        ZoteroSyncRunRead.model_validate(run).model_dump(mode="json")
                        if run is not None
                        else None
                    ),
                }
                _emit(payload, as_json=args.as_json)
                return
            run = await zotero_local.sync_binding(
                session,
                binding=binding,
                requested_by=binding.created_by,
                full=args.full,
            )
            _emit(
                ZoteroSyncRunRead.model_validate(run).model_dump(mode="json"),
                as_json=args.as_json,
            )
    except zotero_local.ZoteroLocalError as exc:
        _emit({"error": exc.code, "detail": exc.detail}, as_json=getattr(args, "as_json", False))
        raise SystemExit(1) from exc
    finally:
        await dispose_engine()


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
