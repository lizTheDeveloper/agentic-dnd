"""Who is calling, and how often.

On the live site these routes sit behind the school's login.  This
standalone version has no accounts, so every browser gets an anonymous
id in its (signed) Flask session cookie.  That id is what makes you the
"creator" of a campaign: only the browser that created it can advance
turns, whisper, or pause it.

Rate limits key on the client IP rather than the session id, because an
anonymous session resets as soon as you clear your cookies.
"""

import uuid
from functools import wraps

from flask import request, session

import store


def require_session(f):
    """Make sure the session has a user_id, minting an anonymous one if not."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            session["user_id"] = f"anon-{uuid.uuid4().hex[:12]}"
        return f(*args, **kwargs)
    return wrapper


def rate_ident():
    return f"ip:{request.remote_addr or '?'}"


def rate_limited(bucket, ident, limit, window):
    """True once `ident` has hit `bucket` more than `limit` times in `window` seconds."""
    try:
        r = store.redis_client
        rl_key = f"dnd:rl:{bucket}:{ident}"
        c = r.incr(rl_key)
        if c == 1:
            r.expire(rl_key, window)
        return c > limit
    except Exception:
        return False
