import inspect
import time
from asyncio import Lock
from functools import wraps
from typing import Callable

from bmslib.cache import to_hashable
from bmslib.util import get_logger

logger = get_logger()


class MemoryCacheStorage:
    def get(self, key):
        raise NotImplementedError()

    def get_default(self, key, returns_default_value: Callable, ttl):
        raise NotImplementedError()

    def set(self, key, value, ttl, ignore_overwrite):
        raise NotImplementedError()

    def __delitem__(self, key):
        raise NotImplementedError()

    def __contains__(self, key):
        raise NotImplementedError();


class DictCacheStorage(MemoryCacheStorage):
    def __init__(self):
        self.d = dict()
        self.time = time.time

    def get(self, key):
        if key not in self:
            return None
        return self.d[key][0]

    def get_default(self, key, returns_default_value: Callable, ttl):
        if key not in self:
            return returns_default_value()
        return self.d[key][0]

    def set(self, key, value, ttl, ignore_overwrite):
        if not ignore_overwrite and key in self:
            logger.warning("overwrite key %s", key)
        self.d[key] = value, (self.time() + ttl)

    def __delitem__(self, key):
        del self.d[key]

    def __contains__(self, key):
        if key not in self.d:
            return False
        return self.d[key][1] >= self.time()


_managed_mem_cache = None


def shared_managed_mem_cache() -> DictCacheStorage:
    global _managed_mem_cache
    if _managed_mem_cache is None:
        _managed_mem_cache = DictCacheStorage()
    return _managed_mem_cache


def mem_cache_deco(ttl, touch=False, ignore_kwargs=None, synchronized=False, expired=None, ignore_rc=False,
                   cache_storage: MemoryCacheStorage = shared_managed_mem_cache(),
                   key_func: Callable = None):
    """
    Decorator
    :param touch: touch key time on hit
    :param ttl:
    :param ignore_kwargs: a set of keyword arguments to ignore when building the cache key
    :param expired Callable to evaluate whether the cached value has expired/invalidated
    :return:
    """

    if ignore_kwargs is None:
        ignore_kwargs = set()

    # ttl = pd.to_timedelta(ttl)
    _mem_cache = cache_storage
    _lock_cache = shared_managed_mem_cache()

    def decorate(target):

        if key_func:
            def _cache_key_obj(args, kwargs):
                return key_func(*args, **kwargs)
        else:
            def _cache_key_obj(args, kwargs):
                kwargs_cache = {k: v for k, v in kwargs.items() if k not in ignore_kwargs}
                return (target, to_hashable(args), to_hashable(kwargs_cache))

        def invalidate(*args, **kwargs):
            del _mem_cache[_cache_key_obj(args, kwargs)]

        setattr(target, "invalidate", invalidate)

        is_coro = inspect.iscoroutinefunction(target)


        @wraps(target)
        def _inner_wrapper(cache_key_obj, args, kwargs):
            ret = _mem_cache.get(cache_key_obj)

            if expired and ret is not None and expired(ret):
                del _mem_cache[cache_key_obj]
                ret = None

            if ret is None:
                ret = target(*args, **kwargs)
                _mem_cache.set(cache_key_obj, ret, ttl=ttl, ignore_overwrite=ignore_rc)
            elif touch:
                _mem_cache.set(cache_key_obj, ret, ttl=ttl, ignore_overwrite=True)

            return ret

        @wraps(target)
        async def _inner_wrapper_async(cache_key_obj, args, kwargs):
            ret = _mem_cache.get(cache_key_obj)

            if expired and ret is not None and expired(ret):
                del _mem_cache[cache_key_obj]
                ret = None

            if ret is None:
                ret = await target(*args, **kwargs)
                _mem_cache.set(cache_key_obj, ret, ttl=ttl, ignore_overwrite=ignore_rc)
            elif touch:
                _mem_cache.set(cache_key_obj, ret, ttl=ttl, ignore_overwrite=True)

            return ret

        if synchronized:
            target_lock = Lock()

            assert not is_coro, "asyncio io not yet supported"

            @wraps(target)
            def _mem_cache_synchronized_wrapper(*args, **kwargs):
                cache_key_obj = _cache_key_obj(args, kwargs)

                with target_lock:
                    lock = _lock_cache.get_default((cache_key_obj, target_lock), Lock, ttl=ttl)

                with lock:
                    return _inner_wrapper(cache_key_obj, args, kwargs)

            return _mem_cache_synchronized_wrapper

        else:
            if is_coro:
                @wraps(target)
                async def _mem_cache_wrapper_async(*args, **kwargs):
                    cache_key_obj = _cache_key_obj(args, kwargs)
                    return await _inner_wrapper_async(cache_key_obj, args, kwargs)

                return _mem_cache_wrapper_async
            else:
                @wraps(target)
                def _mem_cache_wrapper(*args, **kwargs):
                    cache_key_obj = _cache_key_obj(args, kwargs)
                    return _inner_wrapper(cache_key_obj, args, kwargs)

                return _mem_cache_wrapper

    return decorate
