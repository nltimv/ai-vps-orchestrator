from __future__ import annotations

import asyncio
import io
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.api.deps import current_user, get_app, get_session
from aivo.api.schemas import AssetOut
from aivo.context import AppContext
from aivo.db.models import Asset, User
from aivo.services.jobs import asset_out

router = APIRouter(tags=["assets"])

MAX_UPLOAD = 25 * 1024 * 1024
EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/flac": "flac",
    "audio/wav": "wav",
}


@router.post("/uploads", status_code=201)
async def upload(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> AssetOut:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "That image is too large (max 25 MB)")

    def normalize() -> tuple[bytes, int, int]:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            if max(im.size) > 2048:
                im.thumbnail((2048, 2048))
            buf = io.BytesIO()
            im.save(buf, format="PNG")  # re-encode: drops EXIF/GPS metadata
            return buf.getvalue(), im.size[0], im.size[1]

    try:
        png, w, h = await asyncio.to_thread(normalize)
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(422, "That file doesn't look like an image") from e
    aid = uuid.uuid4()
    stored = await asyncio.to_thread(app.store.put_bytes, aid, png)
    a = Asset(
        id=aid,
        owner_user_id=user.id,
        role="input",
        media_type="image/png",
        storage_path=stored.storage_path,
        plaintext_size=stored.plaintext_size,
        ciphertext_size=stored.ciphertext_size,
        key_id=stored.key_id,
        content_mac=stored.content_mac,
        width=w,
        height=h,
    )
    s.add(a)
    await s.commit()
    return asset_out(a)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].partition("-")
    try:
        if a == "":
            n = int(b)
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "Range not satisfiable", headers={"content-range": f"bytes */{size}"})
    return start, end


@router.get("/assets/{asset_id}")
async def get_asset(
    asset_id: uuid.UUID,
    request: Request,
    download: bool = False,
    _: User = Depends(current_user),
    app: AppContext = Depends(get_app),
    s: AsyncSession = Depends(get_session),
) -> Response:
    a = (await s.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "Not found")
    size = a.plaintext_size
    headers = {"accept-ranges": "bytes", "cache-control": "private, max-age=3600", "x-content-type-options": "nosniff"}
    if download:
        headers["content-disposition"] = f'attachment; filename="aivo-{str(a.id)[:8]}.{EXT.get(a.media_type, "bin")}"'
    if size == 0:
        return Response(b"", media_type=a.media_type, headers=headers)
    rng = _parse_range(request.headers.get("range"), size)
    start, end = rng if rng else (0, size - 1)
    headers["content-length"] = str(end - start + 1)
    status = 206 if rng else 200
    if rng:
        headers["content-range"] = f"bytes {start}-{end}/{size}"
    gen = app.store.read_range(a.id, a.storage_path, start, end)

    async def stream():  # type: ignore[no-untyped-def]
        it = iter(gen)
        while True:
            chunk = await asyncio.to_thread(next, it, None)
            if chunk is None:
                break
            yield chunk

    return StreamingResponse(stream(), status_code=status, media_type=a.media_type, headers=headers)
