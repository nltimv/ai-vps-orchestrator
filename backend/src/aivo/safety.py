"""Adult (18+) content rules.

* Who: an account sees and makes adult content only if the admin switched adult content on
  (``RuntimeSettings.adult_content_enabled``) *and* allowed that account (``User.adult_allowed``).
  Admins are no exception. Everyone else never sees adult engines or adult creations: they are left
  out of the catalog, gallery and lists, and their pages and media answer "not found".
* What is adult: everything made with an adult engine, and everything made from an adult picture
  (an edit or animation of it), so it can't be laundered through a normal engine.
* Never: adult engines refuse descriptions that mention minors, and only start from pictures made here
  from a text description, never from an uploaded or edited photo (no explicit images of real people).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aivo.config import RuntimeSettings
from aivo.db.models import Asset, Job, User

NOT_ALLOWED = "Adult content isn't switched on for your account."
REAL_PHOTO = (
    "Adult engines only animate pictures made here from a text description, never uploaded photos or edits of them."
)

# Words that point at minors. Deliberately broad: a refused prompt can be reworded, a wrong
# creation can't be undone. Covers English, Dutch and a few common other languages.
_MINOR_WORDS = [
    "child", "children", "childs", "kid", "kids", "kiddie", "kiddo", "minor", "minors", "underage",
    "under-age", "under age", "teen", "teens", "teenage", "teenaged", "teenager", "teenagers", "preteen",
    "pre-teen", "tween", "tweens", "adolescent", "adolescents", "juvenile", "juveniles", "schoolgirl",
    "schoolgirls", "schoolboy", "schoolboys", "school girl", "school boy", "loli", "lolis", "lolita",
    "lolicon", "shota", "shotacon", "toddler", "toddlers", "infant", "infants", "baby", "babies",
    "little girl", "little girls", "little boy", "little boys", "young girl", "young girls", "young boy",
    "young boys", "girl child", "boy child", "middle school", "elementary school", "primary school",
    "high school", "highschool", "junior high", "kindergarten", "daughter", "daughters", "niece",
    "nephew", "stepdaughter", "jailbait", "cub", "cubs", "pedo", "pedophile", "paedo", "paedophile",
    # Dutch
    "kinderen", "kindje", "kindjes", "meisje", "meisjes", "jongetje", "jongetjes", "tiener", "tieners",
    "minderjarig", "minderjarige", "minderjarigen", "basisschool", "middelbare school", "dochter",
    "dochtertje", "peuter", "kleuter",
    # German, French, Spanish
    "mädchen", "kinder", "minderjährig", "enfant", "enfants", "fillette", "mineure", "niña", "niñas",
    "niño", "niños", "menor", "menores",
]  # fmt: skip
_WORDS = re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in _MINOR_WORDS) + r")(?!\w)", re.IGNORECASE)
_AGE = re.compile(
    r"(?<!\d)([0-9]|1[0-7])\s*(?:-|\s)?\s*(?:yo|y/o|y\.o\.?|yrs?(?:\s*old)?|years?(?:\s*|-)old|years?|jaar|jarige?|"
    r"ans|años|jahre?|jährige?)(?!\w)"
    r"|\baged?\s*([0-9]|1[0-7])(?!\d)",
    re.IGNORECASE,
)


def adult_ok(user: User, rs: RuntimeSettings) -> bool:
    return bool(rs.adult_content_enabled and user.adult_allowed)


def visible(query: Any, user: User, rs: RuntimeSettings) -> Any:
    """Leave adult creations out of a ``select(Job ...)`` for accounts that may not see them."""
    return query if adult_ok(user, rs) else query.where(Job.adult.is_(False))


def can_see(job: Job, user: User, rs: RuntimeSettings) -> bool:
    return not job.adult or adult_ok(user, rs)


def minors_mentioned(text: str) -> str | None:
    """The first word or age in ``text`` that points at a minor, if any."""
    m = _WORDS.search(text) or _AGE.search(text)
    return m.group(0) if m else None


def check_prompt(text: str) -> None:
    hit = minors_mentioned(text or "")
    if hit:
        raise HTTPException(
            422,
            f"This can't be made: adult creations must never involve minors (the description mentions “{hit}”).",
        )


async def adult_rules(
    s: AsyncSession,
    user: User,
    rs: RuntimeSettings,
    *,
    adult_engine: bool,
    prompt: str,
    reference_asset_id: uuid.UUID | None,
) -> bool:
    """Whether a new creation is adult content. Raises (403/422) if it may not be made."""
    ref: Asset | None = None
    ref_job: Job | None = None
    if reference_asset_id:
        ref = (await s.execute(select(Asset).where(Asset.id == reference_asset_id))).scalar_one_or_none()
        if ref is not None and ref.job_id is not None:
            ref_job = (await s.execute(select(Job).where(Job.id == ref.job_id))).scalar_one_or_none()
    adult = adult_engine or bool(ref_job and ref_job.adult)
    if not adult:
        return False
    if not adult_ok(user, rs):
        raise HTTPException(403 if adult_engine else 404, NOT_ALLOWED if adult_engine else "Not found")
    check_prompt(prompt)
    if adult_engine and reference_asset_id is not None:
        made_from_text = (
            ref is not None
            and ref.role == "output"
            and ref_job is not None
            and ref_job.state == "succeeded"
            and ref_job.mode == "t2i"
            and not ref_job.input_asset_ids
        )
        if not made_from_text:
            raise HTTPException(422, REAL_PHOTO)
    return True
