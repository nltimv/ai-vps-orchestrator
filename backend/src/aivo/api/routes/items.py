"""Each user's own gallery: every picture, video and song is a creation of its own, with favorites and collections.

Several may have been made together in one run on the GPU (a job); each still has its own page, favorite,
collections and delete. Only the owner sees them, admins included (aivo/safety.py adds the adult rules).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session
from aivo.api.schemas import (
    BulkIn,
    CollectionIn,
    CollectionOut,
    FavoriteIn,
    ItemCollectionsIn,
    ItemDetailOut,
    ItemOut,
    ItemsPage,
)
from aivo.config import RuntimeSettings
from aivo.context import AppContext
from aivo.controller.jobspec import prompts_used
from aivo.db.models import Asset, Collection, CollectionItem, Job, User
from aivo.safety import visible
from aivo.services.jobs import asset_out, job_out

router = APIRouter(tags=["gallery"])

KINDS = ("image", "video", "music")
DONE = ("succeeded", "failed", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def _encode(job: Job, a: Asset) -> str:
    raw = json.dumps([job.created_at.isoformat(), str(job.id), a.output_name or ""])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, uuid.UUID, str]:
    try:
        at, jid, name = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(at), uuid.UUID(jid), str(name)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "Bad cursor") from e


def _own_items(user: User, rs: RuntimeSettings) -> Any:
    """The user's live outputs with their jobs: newest creation first, those made together in order."""
    q = (
        select(Asset, Job)
        .join(Job, Job.id == Asset.job_id)
        .where(
            Asset.owner_user_id == user.id,
            Job.user_id == user.id,
            Asset.role == "output",
            Asset.deleted_at.is_(None),
            Job.deleted_at.is_(None),
        )
    )
    return visible(q, user, rs)


ORDER = (Job.created_at.desc(), Job.id.desc(), Asset.output_name.asc())


async def build_items(app: AppContext, s: AsyncSession, rows: list[Any]) -> list[ItemOut]:
    if not rows:
        return []
    ids = [a.id for a, _ in rows]
    job_ids = {j.id for _, j in rows}
    thumbs = {
        t.parent_asset_id: t
        for t in (
            await s.execute(
                select(Asset).where(Asset.parent_asset_id.in_(ids), Asset.role == "thumb", Asset.deleted_at.is_(None))
            )
        ).scalars()
    }
    # every output of these jobs, deleted ones too: an item's place among those made together stays put
    together: dict[uuid.UUID, list[tuple[str, uuid.UUID, bool]]] = {}
    for aid, jid, name, gone in (
        await s.execute(
            select(Asset.id, Asset.job_id, Asset.output_name, Asset.deleted_at).where(
                Asset.job_id.in_(job_ids), Asset.role == "output"
            )
        )
    ).all():
        together.setdefault(jid, []).append((name or "", aid, gone is None))
    for v in together.values():
        v.sort()
    prompts = {j.id: app.keyring.open(j.prompt_enc, j.id.bytes) or "" for _, j in rows}
    out = []
    for a, j in rows:
        siblings = together.get(j.id, [])
        out.append(
            ItemOut(
                id=a.id,
                job_id=j.id,
                kind=j.kind,
                index=next((i for i, (_, aid, _) in enumerate(siblings) if aid == a.id), 0),
                together=sum(1 for *_, alive in siblings if alive),
                media=asset_out(a, thumbs.get(a.id)),
                prompt=prompts[j.id],
                favorite=a.favorite_at is not None,
                adult=bool(j.adult),
                created_at=j.created_at,
            )
        )
    return out


async def _own_collection(s: AsyncSession, user: User, cid: uuid.UUID) -> Collection:
    c = (await s.execute(select(Collection).where(Collection.id == cid))).scalar_one_or_none()
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "Collection not found")
    return c


async def _own_item(s: AsyncSession, user: User, rs: RuntimeSettings, item_id: uuid.UUID) -> tuple[Asset, Job]:
    row = (await s.execute(_own_items(user, rs).where(Asset.id == item_id))).first()
    if row is None:
        raise HTTPException(404, "Not found")
    return row[0], row[1]


async def delete_items(app: AppContext, s: AsyncSession, assets: list[Asset]) -> None:
    """Erase these outputs (and their previews). A run with nothing left is deleted too, sealed words and all."""
    if not assets:
        return
    now = _now()
    ids = [a.id for a in assets]
    previews = (
        (await s.execute(select(Asset).where(Asset.parent_asset_id.in_(ids), Asset.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    for a in [*assets, *previews]:
        app.store.delete(a.storage_path)
        a.deleted_at = now
    await s.execute(delete(CollectionItem).where(CollectionItem.asset_id.in_(ids)))
    await s.flush()
    for jid in {a.job_id for a in assets if a.job_id}:
        job = (await s.execute(select(Job).where(Job.id == jid))).scalar_one()
        left = (
            await s.execute(
                select(func.count())
                .select_from(Asset)
                .where(Asset.job_id == jid, Asset.role == "output", Asset.deleted_at.is_(None))
            )
        ).scalar_one()
        if left == 0 and job.state in DONE:
            for rest in (
                (await s.execute(select(Asset).where(Asset.job_id == jid, Asset.deleted_at.is_(None)))).scalars().all()
            ):
                app.store.delete(rest.storage_path)
                rest.deleted_at = now
            job.deleted_at = now
            job.prompt_enc = job.avoid_enc = job.lyrics_enc = job.prompt_used_enc = None


# -- items -------------------------------------------------------------------------


@router.get("/items")
async def list_items(
    kind: str | None = None,
    favorites: bool = False,
    collection: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(40, ge=1, le=100),
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> ItemsPage:
    """The user's gallery, newest first. ``cursor`` continues where the previous page ended."""
    q = _own_items(user, await app.runtime(s))
    if kind in KINDS:
        q = q.where(Job.kind == kind)
    if favorites:
        q = q.where(Asset.favorite_at.is_not(None))
    if collection is not None:
        await _own_collection(s, user, collection)
        q = q.join(CollectionItem, CollectionItem.asset_id == Asset.id).where(
            CollectionItem.collection_id == collection
        )
    if cursor:
        at, jid, name = _decode(cursor)
        q = q.where(
            or_(
                Job.created_at < at,
                and_(Job.created_at == at, Job.id < jid),
                and_(Job.created_at == at, Job.id == jid, Asset.output_name > name),
            )
        )
    rows = (await s.execute(q.order_by(*ORDER).limit(limit + 1))).all()
    page = rows[:limit]
    nxt = _encode(page[-1][1], page[-1][0]) if len(rows) > limit else None
    return ItemsPage(items=await build_items(app, s, page), next_cursor=nxt)


@router.get("/items/{item_id}")
async def get_item(
    item_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> ItemDetailOut:
    a, j = await _own_item(s, user, await app.runtime(s), item_id)
    item = (await build_items(app, s, [(a, j)]))[0]
    used = prompts_used(app, j)
    own = len(used) > 1  # each one made in its own run, with its own description and look number
    improved = (used[item.index] if own and item.index < len(used) else None) or next((t for t in used if t), None)
    seed = (j.resolved or {}).get("seed")
    if own and seed is not None:
        seed = (int(seed) + item.index) % 2**32
    cids = (await s.execute(select(CollectionItem.collection_id).where(CollectionItem.asset_id == a.id))).scalars()
    return ItemDetailOut(
        item=item, job=await job_out(app, s, j), improved=improved, seed=seed, collection_ids=list(cids)
    )


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: uuid.UUID,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    a, _ = await _own_item(s, user, await app.runtime(s), item_id)
    await delete_items(app, s, [a])
    await s.commit()
    return {"ok": True}


@router.put("/items/{item_id}/favorite")
async def set_favorite(
    item_id: uuid.UUID,
    body: FavoriteIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    a, _ = await _own_item(s, user, await app.runtime(s), item_id)
    if body.favorite != (a.favorite_at is not None):
        a.favorite_at = _now() if body.favorite else None
    await s.commit()
    return {"favorite": body.favorite}


@router.put("/items/{item_id}/collections")
async def set_item_collections(
    item_id: uuid.UUID,
    body: ItemCollectionsIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, list[uuid.UUID]]:
    """Put this item in exactly these collections (the user's own)."""
    a, _ = await _own_item(s, user, await app.runtime(s), item_id)
    want = set(body.collection_ids)
    if want:
        mine = set(
            (await s.execute(select(Collection.id).where(Collection.id.in_(want), Collection.user_id == user.id)))
            .scalars()
            .all()
        )
        if mine != want:
            raise HTTPException(404, "Collection not found")
    have = set(
        (await s.execute(select(CollectionItem.collection_id).where(CollectionItem.asset_id == a.id))).scalars().all()
    )
    if have - want:
        await s.execute(
            delete(CollectionItem).where(CollectionItem.asset_id == a.id, CollectionItem.collection_id.in_(have - want))
        )
    for cid in want - have:
        s.add(CollectionItem(collection_id=cid, asset_id=a.id))
    await _touch(s, have ^ want)
    await s.commit()
    return {"collection_ids": sorted(want, key=str)}


@router.post("/items/bulk")
async def bulk(
    body: BulkIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """One action on several of the user's items; ids that aren't theirs are left alone."""
    rows = (await s.execute(_own_items(user, await app.runtime(s)).where(Asset.id.in_(body.ids)))).all()
    assets = [a for a, _ in rows]
    if body.action in ("add", "remove"):
        if body.collection_id is None:
            raise HTTPException(422, "Which collection?")
        await _own_collection(s, user, body.collection_id)
    ids = [a.id for a in assets]
    if body.action == "favorite":
        for a in assets:
            a.favorite_at = a.favorite_at or _now()
    elif body.action == "unfavorite":
        for a in assets:
            a.favorite_at = None
    elif body.action == "delete":
        await delete_items(app, s, assets)
    elif body.action == "add" and ids:
        await s.execute(
            insert(CollectionItem)
            .values([{"collection_id": body.collection_id, "asset_id": i} for i in ids])
            .on_conflict_do_nothing()
        )
        await _touch(s, {body.collection_id})
    elif body.action == "remove" and ids:
        await s.execute(
            delete(CollectionItem).where(
                CollectionItem.collection_id == body.collection_id, CollectionItem.asset_id.in_(ids)
            )
        )
        await _touch(s, {body.collection_id})
    await s.commit()
    return {"count": len(assets)}


async def _touch(s: AsyncSession, ids: set[uuid.UUID | None]) -> None:
    for c in (await s.execute(select(Collection).where(Collection.id.in_([i for i in ids if i])))).scalars():
        c.updated_at = _now()


# -- collections ---------------------------------------------------------------------


async def _collection_out(app: AppContext, s: AsyncSession, user: User, c: Collection) -> CollectionOut:
    rs = await app.runtime(s)
    inside = (
        _own_items(user, rs)
        .join(CollectionItem, CollectionItem.asset_id == Asset.id)
        .where(CollectionItem.collection_id == c.id)
    )
    count = (await s.execute(select(func.count()).select_from(inside.subquery()))).scalar_one()
    newest = (await s.execute(inside.order_by(CollectionItem.added_at.desc()).limit(1))).first()
    cover = None
    if newest:
        media = (await build_items(app, s, [newest]))[0].media
        cover = media.thumb_url or (media.url if media.media_type.startswith("image/") else None)
    return CollectionOut(
        id=c.id, name=c.name, count=count, cover_url=cover, created_at=c.created_at, updated_at=c.updated_at
    )


async def _name_free(s: AsyncSession, user: User, name: str, but: uuid.UUID | None = None) -> str:
    name = " ".join(name.split())
    if not name:
        raise HTTPException(422, "Give the collection a name")
    q = select(Collection.id).where(Collection.user_id == user.id, func.lower(Collection.name) == name.lower())
    if but is not None:
        q = q.where(Collection.id != but)
    if (await s.execute(q)).first():
        raise HTTPException(409, f"You already have a collection called {name}")
    return name


@router.get("/collections")
async def list_collections(
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> list[CollectionOut]:
    cols = (
        (await s.execute(select(Collection).where(Collection.user_id == user.id).order_by(func.lower(Collection.name))))
        .scalars()
        .all()
    )
    return [await _collection_out(app, s, user, c) for c in cols]


@router.post("/collections", status_code=201)
async def create_collection(
    body: CollectionIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> CollectionOut:
    c = Collection(user_id=user.id, name=await _name_free(s, user, body.name))
    s.add(c)
    await s.commit()
    await s.refresh(c)
    return await _collection_out(app, s, user, c)


@router.patch("/collections/{cid}")
async def rename_collection(
    cid: uuid.UUID,
    body: CollectionIn,
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> CollectionOut:
    c = await _own_collection(s, user, cid)
    c.name = await _name_free(s, user, body.name, but=c.id)
    await s.commit()
    await s.refresh(c)
    return await _collection_out(app, s, user, c)


@router.delete("/collections/{cid}")
async def delete_collection(
    cid: uuid.UUID,
    user: User = Depends(current_user),
    s: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """The collection goes; what was in it stays in the gallery."""
    c = await _own_collection(s, user, cid)
    await s.delete(c)
    await s.commit()
    return {"ok": True}
