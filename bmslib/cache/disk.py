from typing import Callable

import pandas as pd

import os
import pickle
import random
import string
from functools import wraps

from bmslib.cache import to_hashable, random_str
from bmslib.util import get_logger

cache_dir = os.path.expanduser('~') + "/.cache/batmon"

logger = get_logger()




def touch(fname, times=None):
    with open(fname, 'a'):
        os.utime(fname, times)


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        import errno
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def _get_fn(key, ext):
    path = cache_dir + "/" + key + "." + ext
    path = os.path.realpath(path)
    dn = os.path.dirname(path)
    # noinspection PyBroadException
    try:
        if not os.path.isdir(dn):
            mkdir_p(dn)
    except:
        pass
    return path


class PickleFileStore:
    def __init__(self):
        pass

    # noinspection PyMethodMayBeStatic
    def read(self, key):
        # noinspection PyBroadException
        try:
            fn = _get_fn(key, ext='pickle')
            with open(fn, 'rb') as fh:
                ret = pickle.load(fh)
            touch(fn)
            return ret
        except:
            return None

    # noinspection PyMethodMayBeStatic
    def write(self, key, df):
        fn = _get_fn(key, ext='pickle')
        s = f'.{random_str(6)}.tmp'
        with open(fn + s, 'wb') as fh:
            pickle.dump(df, fh, pickle.HIGHEST_PROTOCOL)
        os.replace(fn + s, fn)
        # _set_df_file_store_mtime(fn, df)


def func_args_hash_func(target):
    import hashlib
    import inspect
    mod = inspect.getmodule(target)
    path_hash = hashlib.sha224(bytes(mod.__file__, 'utf-8')).hexdigest()[:4]

    def _cache_key(args, kwargs):
        cache_key_obj = (to_hashable(args), to_hashable(kwargs))
        cache_key_hash = hashlib.sha224(bytes(str(cache_key_obj), 'utf-8')).hexdigest()
        return path_hash, cache_key_hash

    return _cache_key


def disk_cache_deco(ignore_kwargs=None):
    if ignore_kwargs is None:
        ignore_kwargs = set()

    disk_cache = PickleFileStore()

    def decorate(target, hash_func_gen=func_args_hash_func):
        import inspect
        mod = inspect.getmodule(target)
        ckh = hash_func_gen(target)

        # noinspection PyBroadException
        @wraps(target)
        def _fallback_cache_wrapper(*args, **kwargs):
            kwargs_cache = {k: v for k, v in kwargs.items() if k not in ignore_kwargs and v is not None}
            k0, k1 = ckh(args, kwargs_cache)
            cache_key_str = '/'.join([mod.__name__, target.__name__ + "__" + k0, k1])

            ret = disk_cache.read(cache_key_str)
            if ret is not None:
                return ret

            try:
                ret = target(*args, **kwargs)
                try:
                    disk_cache.write(cache_key_str, ret)
                    logger.info("wrote %s", cache_key_str)
                except Exception as _e:
                    logger.warning('Fall-back cache: error storing: %s', _e, exc_info=1)
                    pass
            except:
                logger.warning('calling %s (%s) raised an error', target, cache_key_str, exc_info=1)
                raise

            return ret

        return _fallback_cache_wrapper

    return decorate


