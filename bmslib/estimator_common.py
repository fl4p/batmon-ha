"""
Helpers shared by the experimental estimators (`impedance.py`, `qmax.py`):
robust statistics, the LiFePO4 chemistry guard, a fingerprint of the running
code for persisted state, and the validators `restore()` uses.

Kept free of numpy on purpose: the add-on has none.
"""
import functools
import hashlib
import logging
import math
import sys
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


def locked(method):
    """Run the method under self._lock (a threading.RLock). The estimators'
    state is saved from the background thread (main.py, every 30 s) while the
    event loop feeds samples; without the lock a snapshot could land between
    two updates of one add() -- the charge counted but the time not advanced
    -- and a restore from it would count that interval twice."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return math.nan
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


_SCALAR_TYPES = (bool, int, float, complex, str, bytes, type(None))


def _is_const(x) -> bool:
    """Immutable plain data: numbers, strings, bytes, None, and tuples and
    frozensets of them."""
    if isinstance(x, _SCALAR_TYPES) or _is_builtin_type(x):
        return True
    return isinstance(x, (tuple, frozenset)) and all(_is_const(v) for v in x)


# A module names here the module-level containers that are state, not
# configuration: filled while it runs (a cache such as
# impedance._LAG_ORDER_CACHE), so their value when the fingerprint is taken
# says nothing about the code. Every other list, dict or set, at module or
# class level, is configuration and counts (fourth review: a dict of
# tolerances kept the fingerprint whatever it held). An explicit list, so
# that the safe side is the default.
STATE_NAMES_ATTR = '_FINGERPRINT_STATE'


def _is_builtin_type(x) -> bool:
    """int, float, type(None), ...: as data (_SCALAR_TYPES) they are names."""
    return isinstance(x, type) and x.__module__ == 'builtins'


# Python 3.14 marks a code object whose co_consts[0] is the docstring. Masked
# out with the docstring itself (below).
_CO_HAS_DOCSTRING = 0x4000000

# Class attributes that are not code: the class docstring, and the line the
# class starts on (__firstlineno__, 3.13+). __static_attributes__ (3.13+) is
# derived from the methods' code, which is hashed itself.
_CLASS_SKIP = frozenset(('__doc__', '__firstlineno__', '__static_attributes__', '__module__', '__qualname__',
                         '__dict__', '__weakref__'))


def _canon(x, doc=None):
    """A canonical, hashable description of x built only from tuples, strings,
    bytes and numbers, whose repr() is the same in every process of a given
    Python version.

    Not marshal: its bytes depend on more than the value -- whether a string
    happens to be interned (type byte 0xda vs 0xfa on 3.10, which changed with
    what else was imported: enabling impedance_estimator changed the Qmax
    fingerprint) and, for sets on 3.10, the hash seed.

    For a code object: the bytecode, constants, names, argument layout and
    flags; NOT its line numbers or positions (co_firstlineno, the line table),
    its file name, or its docstring (doc, replaced by a marker), so that a
    comment, a docstring or a moved block keeps saved state.

    Raises TypeError for anything else: what cannot be described is never
    fingerprinted as if it were known."""
    if isinstance(x, types.CodeType):
        consts = list(x.co_consts)
        if doc is not None and consts and consts[0] == doc:
            consts[0] = '<docstring>'
        return ('<code>', x.co_name, x.co_argcount, x.co_posonlyargcount, x.co_kwonlyargcount,
                x.co_flags & ~_CO_HAS_DOCSTRING, x.co_code, getattr(x, 'co_exceptiontable', b''),
                tuple(_canon(c) for c in consts), x.co_names, x.co_varnames, x.co_freevars, x.co_cellvars)
    if isinstance(x, bool) or x is None or x is Ellipsis:
        return repr(x)
    if _is_builtin_type(x):
        return ('<type>', x.__qualname__)
    if isinstance(x, _SCALAR_TYPES):
        return (type(x).__name__, repr(x))  # float repr is exact; the tag keeps 1, 1.0 and '1' apart
    if isinstance(x, tuple):
        return ('<tuple>',) + tuple(_canon(v) for v in x)
    if isinstance(x, (frozenset, set)):
        return ('<set>',) + tuple(sorted((_canon(v) for v in x), key=repr))
    if isinstance(x, list):  # function defaults only; module-level lists are state (_is_const)
        return ('<list>',) + tuple(_canon(v) for v in x)
    if isinstance(x, dict):
        return ('<dict>',) + tuple(sorted(((_canon(k), _canon(v)) for k, v in x.items()), key=repr))
    if isinstance(x, slice):  # a constant in 3.14 bytecode (x[:3])
        return ('<slice>', _canon(x.start), _canon(x.stop), _canon(x.step))
    raise TypeError('cannot fingerprint %s' % type(x).__name__)


def _put(h, *parts):
    h.update(repr(parts).encode() + b'\0')


def _canon_fn(f, seen):
    """A Python function as code: its code object (docstring masked), its
    defaults and the contents of its closure cells, each through _canon_obj --
    a closure is how a factory (_mk(3)), a lambda over a value or a decorator
    with arguments (@_tol(1.0)) configures a function, and the code alone is
    the same for every value. A function met again on the way (a recursive
    closure) is named, not walked."""
    if id(f) in seen:
        return ('<recursive>', f.__qualname__)
    seen = seen | {id(f)}
    cells = []
    for name, cell in zip(f.__code__.co_freevars, f.__closure__ or ()):
        try:
            v = cell.cell_contents
        except ValueError:
            cells.append((name, '<empty>'))
            continue
        if name == '__class__' and isinstance(v, type):
            cells.append((name, ('<class>', v.__module__, v.__qualname__)))  # super(): the class is walked itself
        else:
            cells.append((name, _canon_obj(v, seen)))
    return ('<func>', f.__qualname__, _canon(f.__code__, f.__doc__), _canon_obj(f.__defaults__, seen),
            _canon_obj(f.__kwdefaults__, seen), tuple(cells))


def _canon_obj(x, seen=frozenset()):
    """_canon, extended to what a closure cell or a default can hold:
    functions (walked, _canon_fn), modules and imported C functions (by name:
    fingerprinted as their own module, or as the interpreter's code), the
    logger (configures nothing), configured objects (their
    fingerprint_data()), and containers of those. Anything else raises
    TypeError: what cannot be described is never taken as known."""
    if isinstance(x, (staticmethod, classmethod)):
        x = x.__func__
    if isinstance(x, types.FunctionType):
        return _canon_fn(x, seen)
    if isinstance(x, types.ModuleType):
        return ('<module>', x.__name__)
    if isinstance(x, types.BuiltinFunctionType) and (x.__self__ is None or isinstance(x.__self__, types.ModuleType)):
        return ('<builtin>', getattr(x, '__module__', None), x.__qualname__)
    if isinstance(x, logging.Logger):
        return ('<logger>',)
    if isinstance(x, (list, tuple)):
        return ('<%s>' % type(x).__name__,) + tuple(_canon_obj(v, seen) for v in x)
    if isinstance(x, (set, frozenset)):
        return ('<set>',) + tuple(sorted((_canon_obj(v, seen) for v in x), key=repr))
    if isinstance(x, dict):
        return ('<dict>',) + tuple(sorted(((_canon_obj(k, seen), _canon_obj(v, seen)) for k, v in x.items()),
                                          key=repr))
    if not isinstance(x, type) and callable(getattr(x, 'fingerprint_data', None)):
        return ('<object>', type(x).__qualname__, _canon(x.fingerprint_data()))
    return _canon(x)


def _imported(f) -> bool:
    """A function defined in another module and bound here under import: the
    very object its module holds. A function made there and bound here
    (a closure from a factory, partial-like wrappers) is not: its closure is
    this module's configuration."""
    home = sys.modules.get(f.__module__)
    return home is not None and getattr(home, f.__name__, None) is f


def _feed(h, name: str, obj, modname: str, depth: int = 0):
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    if isinstance(obj, property):
        for k, f in (('get', obj.fget), ('set', obj.fset), ('del', obj.fdel)):
            if f is not None:
                _feed(h, name + '.' + k, f, modname, depth)
    elif isinstance(obj, types.FunctionType):
        if obj.__module__ != modname and _imported(obj):
            return  # imported: fingerprinted with its own module
        _put(h, 'def', name, _canon_fn(obj, frozenset()))
        if hasattr(obj, '__wrapped__'):  # a decorated method (locked): its body is the wrapped function
            _feed(h, name + '.__wrapped__', obj.__wrapped__, modname, depth)
    elif isinstance(obj, type):
        if obj.__module__ != modname or depth > 3:
            return
        _put(h, 'class', name)
        for k in sorted(vars(obj)):
            if k not in _CLASS_SKIP:
                _feed(h, name + '.' + k, vars(obj)[k], modname, depth + 1)
    elif isinstance(obj, types.ModuleType):
        return  # an imported module: fingerprinted when its namespace is passed too
    elif isinstance(obj, types.BuiltinFunctionType) and (obj.__self__ is None
                                                          or isinstance(obj.__self__, types.ModuleType)):
        return  # an imported C function (math.floor, bisect.bisect_left): the interpreter's code, whose version counts
    elif _is_const(obj):
        _put(h, 'const', name, _canon(obj))
    elif isinstance(obj, (list, dict, set)):
        _put(h, 'container', name, _canon_obj(obj))  # configuration (STATE_NAMES_ATTR names the state)
    elif callable(getattr(obj, 'fingerprint_data', None)) and not isinstance(obj, type):
        # A configured object, e.g. qmax.DEFAULT_CURVE = OcvCurve(...): its
        # class's code is fingerprinted as a class, but the arguments it was
        # built with are not code. It says what configures it.
        _put(h, 'object', name, type(obj).__qualname__, _canon(obj.fingerprint_data()))
    elif isinstance(obj, (logging.Logger, types.MemberDescriptorType, types.GetSetDescriptorType)) \
            or type(obj).__module__ in ('typing', 'typing_extensions'):
        # the logger, a class's __slots__ entries (the tuple itself counts),
        # type annotations (Optional, Dict, ...): configure nothing
        return
    else:
        # Anything else might configure behaviour, and what is not described
        # cannot be vouched for: the fingerprint becomes None (never a match).
        raise TypeError('cannot fingerprint module-level %s (%s)' % (name, type(obj).__name__))


def code_fingerprint(*namespaces) -> Optional[str]:
    """Fingerprint of the code that is RUNNING: the code objects of every
    function and class defined in the given module namespaces (a module's
    __dict__, or globals() from inside it), with their defaults, and every
    module-level constant (immutable plain data: numbers, strings, tuples of
    them). Persisted results are only restored into the code that computed
    them.

    What changes it: the bytecode of any function or method, their constants
    and defaults, the contents of their closures (a factory's argument, a
    decorator's), a module-level or class-level constant or container, a
    name, a module-level object that configures behaviour (what its
    fingerprint_data() returns: for qmax.DEFAULT_CURVE the curve's data, its
    parameters and the tables built from them), and the Python version (the
    same bytecode is not the same program on another interpreter). What does
    not: comments, docstrings, blank lines, where a block sits in the file
    (line numbers), the install path, the hash seed, what else was imported,
    and the module-level containers the module names as state
    (STATE_NAMES_ATTR).

    Why not a hash of the .py file: Python may execute a timestamp-valid .pyc
    compiled from different source, and the file hash then vouches for code
    that is not running. Imported names count with the module that defines
    them, which must be passed too. Of the other module-level objects only the
    logger and type annotations are left out; any other object without a
    fingerprint_data() makes the fingerprint None. An object built by
    fingerprinted code is not therefore fingerprinted: OcvCurve(sigma=1.0)
    runs the same code as OcvCurve() and is another curve.

    Call it at the END of the module, once everything is defined. None when
    anything cannot be fingerprinted: unknown code never matches, so nothing
    is restored as if it did."""
    h = hashlib.sha1()
    try:
        _put(h, 'python', tuple(sys.version_info[:2]))
        for ns in namespaces:
            modname = ns['__name__']
            _put(h, 'module', modname)
            state = ns.get(STATE_NAMES_ATTR, frozenset())
            if not (isinstance(state, frozenset) and all(isinstance(n, str) for n in state)):
                raise TypeError('%s.%s must be a frozenset of names' % (modname, STATE_NAMES_ATTR))
            for name in sorted(ns):
                if not name.startswith('__') and name != 'CODE_FINGERPRINT' and name not in state:
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
