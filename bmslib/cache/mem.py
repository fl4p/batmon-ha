import time
from functools import wraps

from bmslib.util import get_logger

logger = get_logger()


def mem_cache_deco(ttl):
    """
    Cache an async function's result for `ttl` seconds, keyed by its (hashable) positional args, so a method is
    cached per instance. Exceptions and None results are not cached. `fn.invalidate(*args)` drops an entry
    (KeyError if it was never stored).
    """

    def decorate(target):
        cache = {}  # args -> (value, expiry)

        def invalidate(*args):
            del cache[args]

        @wraps(target)
        async def wrapper(*args):
            hit = cache.get(args)
            if hit is not None and hit[1] >= time.monotonic():
                return hit[0]
            ret = await target(*args)
            if ret is not None:
                if args in cache and cache[args][1] >= time.monotonic():
                    logger.warning("overwrite key %s", (target, args))
                cache[args] = ret, time.monotonic() + ttl
            return ret

        wrapper.invalidate = invalidate
        return wrapper

    return decorate
