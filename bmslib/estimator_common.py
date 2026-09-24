"""
Helpers shared by the experimental estimators (`impedance.py`, `qmax.py`):
robust statistics, the LiFePO4 chemistry guard, a fingerprint of the running
code for persisted state, and the validators `restore()` uses.

Kept free of numpy on purpose: the add-on has none.
"""
import hashlib
import marshal
import math
import time
import types
from typing import Optional, Sequence, Tuple, TypeGuard

# Chemistry guard: only LFP-looking packs. A sample with any cell outside this
# band is dropped; an estimator is disabled for the BMS (warning, once) when the
# median cell voltage stays outside it for CHEM_PERSIST_S seconds AND
# CHEM_PERSIST_N samples in a row.
# Why 10 minutes / 30 samples: the out-of-band readings seen on real LFP data
# are single decode glitches lasting a few frames (2023-11-14: 3732/3119 mV,
# then 3329/3512/2798/2926, then normal, within 5 s) and single runner cells
# at the top of a charge, which never move the median. A non-LFP pack (NMC
# rests at 3.7-4.1 V per cell) sits outside the band for hours, so waiting 10
# minutes costs nothing. The sample count keeps two readings either side of a
# 10-minute outage from counting as "persistent".
LFP_MV_LO, LFP_MV_HI = 2500.0, 3700.0
CHEM_PERSIST_S = 600.0
CHEM_PERSIST_N = 30

# Input plausibility of the pack current: a reading above this is a decode
# glitch, not a current. Seen in real JK telemetry: 2 147 483.136 A (about
# 2^31 mA), which a trapezoid between 15 s samples turns into 8 948 Ah. Bound:
# I_MAX_C_RATE times the capacity when one is known, never above I_MAX_ABS_A.
# LFP energy cells like those in these packs are specified for about 1C
# continuous; 5C leaves margin for a small pack behind a big inverter. 1000 A
# is above the rating of the BMSes the add-on reads (a few hundred amps). A
# real current above the bound costs a segment or a resistance pair, never a
# wrong number, so the bound errs high. What it cannot catch: a glitch below
# it, worth at most 5C x one sample interval (0.14 % of C at the default 1 s,
# 2 % at 15 s).
I_MAX_C_RATE = 5.0
I_MAX_ABS_A = 1000.0


def finite(x) -> TypeGuard[float]:
    return isinstance(x, (int, float)) and math.isfinite(x)


def fmt_t(t: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(t))


def current_ceiling(capacity: Optional[float], c_rate: float = I_MAX_C_RATE,
                    abs_max: float = I_MAX_ABS_A) -> float:
    """Largest |current| [A] taken as a measurement: min(abs_max, c_rate *
    capacity), abs_max without a usable capacity. The caller passes its own
    module's constants (as for chemistry_step)."""
    if finite(capacity) and capacity > 0:
        return min(abs_max, c_rate * capacity)
    return abs_max


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return math.nan
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


_CONST_TYPES = (bool, int, float, complex, str, bytes, type(None))


def _is_const(x) -> bool:
    """Plain data: numbers, strings, and tuples/lists/sets/dicts of them."""
    if isinstance(x, _CONST_TYPES):
        return True
    if isinstance(x, dict):
        return all(_is_const(k) and _is_const(v) for k, v in x.items())
    return isinstance(x, (tuple, list, frozenset, set)) and all(_is_const(v) for v in x)


def _canon(x):
    """x with every set replaced by a sorted tuple. Python 3.10 (which the
    Dockerfile still accepts) marshals a set of strings in hash order, which
    changes with PYTHONHASHSEED, so the same code would fingerprint
    differently in every process; 3.12 and later sort."""
    if isinstance(x, types.CodeType):
        return _norm_code(x)
    if isinstance(x, (set, frozenset)):
        return ('<set>',) + tuple(sorted((_canon(v) for v in x), key=repr))
    if isinstance(x, tuple):
        return tuple(_canon(v) for v in x)
    if isinstance(x, list):
        return [_canon(v) for v in x]
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in x.items()}
    return x


def _norm_code(co: types.CodeType) -> types.CodeType:
    """The code object as it runs, minus what is not code: the file it was
    loaded from (an install path), and set constants in hash order (_canon).
    Nested code objects (closures, comprehensions) too. Only ever hashed,
    never executed."""
    return co.replace(co_filename='', co_consts=tuple(_canon(c) for c in co.co_consts))


def _feed(h, name: str, obj, modname: str, depth: int = 0):
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    if isinstance(obj, property):
        for k, f in (('get', obj.fget), ('set', obj.fset), ('del', obj.fdel)):
            if f is not None:
                _feed(h, name + '.' + k, f, modname, depth)
    elif isinstance(obj, types.FunctionType):
        if obj.__module__ != modname:
            return  # imported: fingerprinted with its own module
        h.update(name.encode() + b'\0')
        h.update(marshal.dumps((_norm_code(obj.__code__), _canon(obj.__defaults__), _canon(obj.__kwdefaults__))))
    elif isinstance(obj, type):
        if obj.__module__ != modname or depth > 3:
            return
        h.update(b'class ' + name.encode() + b'\0')
        for k in sorted(vars(obj)):
            _feed(h, name + '.' + k, vars(obj)[k], modname, depth + 1)
    elif _is_const(obj):
        h.update(name.encode() + b'=' + marshal.dumps(_canon(obj)))


def code_fingerprint(*namespaces) -> Optional[str]:
    """Fingerprint of the code that is RUNNING: the marshalled code objects of
    every function and class defined in the given module namespaces (a
    module's __dict__, or globals() from inside it), with their defaults, and
    every module-level constant (plain data: numbers, strings, containers of
    them, as they are when the call is made). Persisted results are only
    restored into the code that computed them.

    Why not a hash of the .py file: Python may execute a timestamp-valid .pyc
    compiled from different source, and the file hash then vouches for code
    that is not running. Imported names count with the module that defines
    them, which must be passed too. Objects that are not plain data (the
    logger, an OcvCurve instance) are left out; they are built by fingerprinted
    code from fingerprinted constants.

    Call it at the END of the module, once everything is defined. None when
    anything cannot be fingerprinted: unknown code never matches, so nothing
    is restored as if it did. The marshal format depends on the Python
    version, so an interpreter upgrade also discards saved state."""
    h = hashlib.sha1()
    try:
        for ns in namespaces:
            modname = ns['__name__']
            h.update(b'module ' + modname.encode() + b'\0')
            for name in sorted(ns):
                if not name.startswith('__') and name != 'CODE_FINGERPRINT':
                    _feed(h, name, ns[name], modname)
    except Exception:
        return None
    return h.hexdigest()[:16]


def chemistry_step(oob_since: Optional[float], oob_n: int, t: float, finite_mv: Sequence[float],
                   lo: float = LFP_MV_LO, hi: float = LFP_MV_HI, persist_s: float = CHEM_PERSIST_S,
                   persist_n: int = CHEM_PERSIST_N) -> Tuple[Optional[float], int, str, float]:
    """One step of the chemistry guard for the finite cell voltages of a sample.

    Returns (oob_since, oob_n, verdict, median_mv), verdict one of
      'ok'      -- every cell inside lo..hi, use the sample
      'drop'    -- some cell outside (a glitch, a runner cell): skip its voltages
      'disable' -- the median has been outside for persist_s AND persist_n samples
    The caller passes its own module's band constants, so a test that moves
    them on one estimator module moves them for that estimator only."""
    med = median(finite_mv)
    if lo <= med <= hi:
        oob_since, oob_n = None, 0
    else:
        if oob_since is None:
            oob_since = t
        oob_n += 1
        if t - oob_since >= persist_s and oob_n >= persist_n:
            return oob_since, oob_n, 'disable', med
    if min(finite_mv) < lo or max(finite_mv) > hi:
        return oob_since, oob_n, 'drop', med
    return oob_since, oob_n, 'ok', med


# --- validators for restore(): raise ValueError, never return a bad value ---

def v_fin(x, what) -> float:
    if not finite(x) or isinstance(x, bool):
        raise ValueError('%s is %r' % (what, x))
    return float(x)


def v_opt_fin(x, what) -> Optional[float]:
    return None if x is None else v_fin(x, what)


def v_int(x, what, lo=0) -> int:
    if not isinstance(x, int) or isinstance(x, bool) or x < lo:
        raise ValueError('%s is %r' % (what, x))
    return x
