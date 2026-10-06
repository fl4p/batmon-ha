"""Shared estimator helpers (bmslib/estimator_common.py): the code fingerprint
that decides whether persisted estimator state is restored."""
import os
import shutil
import subprocess
import sys

import pytest

import bmslib.estimator_common as ec

BMSLIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py(cwd, code, *flags, seed='0'):
    env = {k: v for k, v in os.environ.items() if k not in ('SOURCE_DATE_EPOCH', 'PYTHONDONTWRITEBYTECODE')}
    env.update(PYTHONPATH=str(cwd), PYTHONHASHSEED=seed)
    r = subprocess.run([sys.executable, *flags, '-c', code], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def _copy_bmslib(dst):
    pkg = dst / 'bmslib'
    pkg.mkdir()
    for f in ('__init__.py', 'estimator_common.py', 'qmax.py', 'impedance.py', 'util.py', 'bms.py'):
        shutil.copy(os.path.join(BMSLIB, f), pkg / f)
    return pkg


@pytest.mark.parametrize('mod,old,new', [('qmax', 'MIN_DSOC = 60.0', 'MIN_DSOC = 61.0'),
                                         ('impedance', 'MIN_R2 = 0.80', 'MIN_R2 = 0.81')])
def test_a_stale_pyc_is_fingerprinted_as_the_code_that_runs(tmp_path, mod, old, new):
    """The reviewer's scenario on the real modules: compile, then edit the
    source to the same length and keep its mtime. Python runs the old .pyc, so
    the fingerprint must be the OLD code's -- a hash of the file on disk would
    vouch for the new source, which is not running."""
    pkg = _copy_bmslib(tmp_path)
    name = old.split()[0]
    probe = 'import bmslib.%s as m; print(m.CODE_FINGERPRINT, m.%s)' % (mod, name)
    fp_old, v_old = _py(tmp_path, probe)  # also writes the .pyc
    src = pkg / (mod + '.py')
    st = src.stat()
    text = src.read_text()
    assert text.count(old) == 1 and len(new) == len(old)
    src.write_text(text.replace(old, new))
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))

    fp_stale, v_stale = _py(tmp_path, probe)
    assert v_stale == v_old, 'precondition: the stale .pyc must be what runs'
    shutil.rmtree(pkg / '__pycache__')
    fp_new, v_new = _py(tmp_path, probe, '-B')
    assert v_new != v_old
    assert fp_stale == fp_old  # what runs is the old code, and the fingerprint says so
    assert fp_new != fp_stale


def test_the_fingerprint_does_not_depend_on_the_hash_seed_or_the_install_path(tmp_path):
    """Python 3.10 marshals sets in hash order; without sorting them the same
    code would get a new fingerprint in every process and never restore."""
    for sub in ('a', 'b'):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / 'setmod.py').write_text(
            'from bmslib.estimator_common import code_fingerprint\n'
            "WORDS = {'alpha', 'beta', 'gamma', 'delta', 'epsilon'}\n"
            'def f(x):\n'
            "    return x in {'p', 'q', 'r', 's', 't', 'u'}\n"
            'CODE_FINGERPRINT = code_fingerprint(globals())\n')
    probe = 'import setmod; print(setmod.CODE_FINGERPRINT)'
    fps = set()
    for sub in ('a', 'b'):
        for seed in ('1', '2', '3', '4'):
            env_path = '%s%s%s' % (tmp_path / sub, os.pathsep, os.path.dirname(BMSLIB))
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
            env.update(PYTHONPATH=env_path, PYTHONHASHSEED=seed)
            r = subprocess.run([sys.executable, '-B', '-c', probe], env=env, capture_output=True, text=True,
                               timeout=60)
            assert r.returncode == 0, r.stderr
            fps.add(r.stdout.strip())
    assert len(fps) == 1 and None not in fps and 'None' not in fps


def test_calibration_set_order_depends_on_the_hash_seed():
    """Known-bad for the set sorting in _canon: iterating a set of strings
    (what repr() and an unsorted walk do) follows the hash seed, on every
    Python version. Without sorting, the same code would fingerprint
    differently in every process and never restore."""
    code = "print(repr(frozenset(['alpha', 'beta', 'gamma', 'delta', 'epsilon'])))"
    outs = set()
    for seed in ('1', '2', '3', '4', '5', '6'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                                timeout=60).stdout)
    assert len(outs) > 1


def _interpreters():
    """This interpreter, and Python 3.10 when installed: the Dockerfile still
    accepts 3.10, and that is where marshal's bytes changed with the import
    order (finding of the second review, 2026-09-24)."""
    out = [sys.executable]
    p310 = shutil.which('python3.10')
    if p310 and sys.version_info[:2] != (3, 10):
        out.append(p310)
    return out


@pytest.mark.parametrize('exe', _interpreters())
def test_the_fingerprint_does_not_depend_on_what_else_was_imported(exe):
    """Enabling impedance_estimator imports impedance.py before qmax.py. On
    3.10 that changed the Qmax fingerprint (a string constant interned or not,
    0xda vs 0xfa in marshal's output), so turning one estimator on discarded
    the other's saved state."""
    fps = set()
    for pre in ('', 'import bmslib.impedance; ', 'import bmslib.estimator_common, bmslib.impedance; '):
        r = subprocess.run([exe, '-B', '-c', pre + 'import bmslib.qmax as q, bmslib.impedance as i; '
                            'print(q.CODE_FINGERPRINT, i.CODE_FINGERPRINT)'],
                           cwd=os.path.dirname(BMSLIB), capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=os.path.dirname(BMSLIB)))
        assert r.returncode == 0, r.stderr
        fps.add(r.stdout.strip())
    assert len(fps) == 1 and 'None' not in fps.pop()


def test_calibration_marshal_depends_on_the_import_order_on_python_310():
    """Known-bad for hashing marshal's bytes: on 3.10 the same function
    (fit_relaxation) marshals differently when impedance.py was imported
    first (measured 2026-09-24 with /usr/local/bin/python3.10)."""
    p310 = shutil.which('python3.10')
    if not p310:
        pytest.skip('no python3.10 on PATH to calibrate against')
    outs = set()
    for pre in ('', 'import bmslib.impedance; '):
        r = subprocess.run([p310, '-B', '-c', pre + 'import marshal, bmslib.qmax as q; '
                            'print(marshal.dumps(q.fit_relaxation.__code__).hex())'],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=os.path.dirname(BMSLIB)))
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout)
    assert len(outs) == 2


@pytest.mark.parametrize('mod', ['qmax', 'impedance'])
def test_comments_docstrings_and_moved_lines_keep_the_fingerprint_logic_does_not(tmp_path, mod):
    """Line numbers were part of it (co_firstlineno, the line table, a
    class's __firstlineno__), so a comment-only edit discarded months of Qmax
    segments. Every edit below shifts every line after it."""
    pkg = _copy_bmslib(tmp_path)
    src = pkg / (mod + '.py')
    text = src.read_text()
    probe = 'import bmslib.%s as m; print(m.CODE_FINGERPRINT)' % mod

    def fp(new_text):
        src.write_text(new_text)
        shutil.rmtree(pkg / '__pycache__', ignore_errors=True)
        out, = _py(tmp_path, probe, '-B')
        return out

    base = fp(text)
    assert base != 'None'
    anchor = '\nlogger = get_logger()\n'
    assert text.count(anchor) == 1
    cls = 'class QmaxEstimator:\n' if mod == 'qmax' else 'class CellResistanceEstimator:\n'
    doc = '"""Streaming per-BMS estimator. Feed add() once per sampler iteration."""'
    meth = '    def _restore(self, st):\n'
    fdoc = '"""Feed one sampler iteration.'  # a method's docstring: its code object's first constant
    assert text.count(cls) == 1 and text.count(doc) == 1 and text.count(meth) == 1 and text.count(fdoc) == 1
    cosmetic = (text.replace(anchor, anchor + '# a comment\n\n\n# and another\n')
                .replace(cls, '# a comment above the class\n' + cls)
                .replace(doc, '"""Streaming estimator, per BMS. Feed add()\n    once per sampler iteration."""')
                .replace(fdoc, '"""Feed one sampler iteration (reworded).')
                .replace(meth, meth + '        # a comment inside a method\n'))
    assert fp(cosmetic) == base
    for old, new in ((' >= MIN_REST_S:', ' > MIN_REST_S:'), ('MIN_R2 = 0.80', 'MIN_R2 = 0.81'),
                     ('if not r2 >= MIN_R2:', 'if not r2 > MIN_R2:')):
        if text.count(old) == 1:
            assert fp(text.replace(old, new)) != base, old
            break
    else:
        pytest.fail('no logic edit applied')


def _own_lag_cache(monkeypatch):
    import bmslib.impedance as imp
    monkeypatch.setattr(imp, '_LAG_ORDER_CACHE', dict(imp._LAG_ORDER_CACHE))  # restored after the test
    assert 7 not in imp._LAG_ORDER_CACHE
    return imp


def test_module_state_is_not_fingerprinted(monkeypatch):
    """impedance._LAG_ORDER_CACHE is filled at run time. It was hashed as a
    constant, so the fingerprint depended on when it was computed. The module
    names it as state (_FINGERPRINT_STATE)."""
    imp = _own_lag_cache(monkeypatch)
    before = ec.code_fingerprint(vars(imp), vars(ec))
    imp._best_lag_diffs([0.1] * 30, [0.2] * 30, max_lag=7)  # a new cache entry
    assert 7 in imp._LAG_ORDER_CACHE  # precondition: it is state, and it changed
    assert ec.code_fingerprint(vars(imp), vars(ec)) == before == imp.CODE_FINGERPRINT


def test_calibration_a_mutable_container_counted_as_a_constant_follows_the_state(monkeypatch):
    imp = _own_lag_cache(monkeypatch)
    monkeypatch.setattr(imp, '_FINGERPRINT_STATE', frozenset())  # not named as state: configuration
    before = ec.code_fingerprint(vars(imp), vars(ec))
    assert before is not None
    imp._best_lag_diffs([0.1] * 30, [0.2] * 30, max_lag=7)
    assert ec.code_fingerprint(vars(imp), vars(ec)) != before


def test_the_estimators_name_their_run_time_state_and_nothing_else():
    """Module-level containers are configuration unless the module names them
    as state; what it names must be a container that is filled at run time,
    never configuration. A new one fails here and needs that decision."""
    import bmslib.impedance as imp
    import bmslib.qmax as q
    found = {m.__name__ + '.' + k for m in (q, imp, ec) for k, v in vars(m).items()
             if not k.startswith('__') and isinstance(v, (list, dict, set, bytearray))}
    assert found == {'bmslib.impedance._LAG_ORDER_CACHE'}
    named = {m.__name__ + '.' + k for m in (q, imp, ec) for k in getattr(m, ec.STATE_NAMES_ATTR, ())}
    assert named == found


# ---------------------------------------------------------------- closures and containers (fourth review)

FP_HOLES = [
    # (what, source with {V}, a, b) -- each pair must fingerprint differently
    ('closure', 'def _mk(k):\n    def f(x):\n        return x * k\n    return f\n_SCALE = _mk({V})\n', '3', '4'),
    ('lambda closure', '_F = (lambda k: (lambda x: x * k))({V})\n', '3', '4'),
    ('decorator argument', 'import functools as _ft\ndef _tol(x):\n    def deco(f):\n        @_ft.wraps(f)\n'
     '        def w(*a):\n            return f(*a) * x\n        return w\n    return deco\n@_tol({V})\n'
     'def _g(v):\n    return v\n', '1.0', '1.1'),
    ('class dict', 'class _E:\n    _CFG = {{"tol": {V}}}\n', '0.02', '0.03'),
    ('class list', 'class _E:\n    _TBL = [{V}, 2]\n', '1', '5'),
    ('module dict', '_CFG = {{"tol": {V}}}\n', '0.02', '0.03'),
    ('module list', '_TBL = [{V}, 2]\n', '1', '5'),
    ('closure over a closure', 'def _mk(k):\n    def g(x):\n        return x * k\n    def f(x):\n'
     '        return g(x) + 1\n    return f\n_SCALE = _mk({V})\n', '3', '4'),
    ('closure default', 'def _mk(k):\n    def f(x, y=k):\n        return x * y\n    return f\n_SCALE = _mk({V})\n',
     '3', '4'),
    # rev8: kept the fingerprint whatever value they held
    ('function attribute', 'def _gate(x):\n    return x < _gate.lim\n_gate.lim = {V}\n', '5.0', '6.0'),
    ('method attribute', 'class _K:\n    def m(self):\n        return self.m.__func__.lim\n_K.m.lim = {V}\n', '1', '2'),
    ('docstring equal to a returned constant', 'def _mode():\n    "{V}"\n    return "{V}"\n', 'chg', 'dch'),
]


def _fp_src(src):
    ns = {'__name__': 'fake_mod'}
    exec(compile(src, 'fake.py', 'exec'), ns)
    ns.pop('__builtins__', None)
    return ec.code_fingerprint(ns)


@pytest.mark.parametrize('what,src,a,b', FP_HOLES, ids=[h[0] for h in FP_HOLES])
def test_closures_and_containers_are_configuration(what, src, a, b):
    """Each kept the fingerprint whatever value it held (fourth review):
    the code is the same, the configuration is in a closure cell or a
    container."""
    fa, fb = _fp_src(src.format(V=a)), _fp_src(src.format(V=b))
    assert fa is not None and fb is not None
    assert fa != fb
    assert _fp_src(src.format(V=a)) == fa  # and stable


def test_calibration_without_the_closure_cells_the_factory_argument_is_unseen(monkeypatch):
    orig = ec._canon_fn
    monkeypatch.setattr(ec, '_canon_fn', lambda f, seen: orig(_no_closure(f), seen))
    for what, src, a, b in FP_HOLES:
        if ('closure' in what or 'decorator' in what) and what != 'closure default':  # (that one: __defaults__)
            assert _fp_src(src.format(V=a)) == _fp_src(src.format(V=b)), what


def _no_closure(f):
    import types
    return types.FunctionType(f.__code__, f.__globals__, f.__name__, f.__defaults__,
                              tuple(types.CellType(None) for _ in f.__code__.co_freevars) or None)


def test_a_closure_made_in_another_module_is_configuration_here(tmp_path, monkeypatch):
    """A factory imported from elsewhere: the function it returns has the
    other module's __module__ and used to be skipped as imported."""
    import sys
    import types
    other = types.ModuleType('fake_factory')
    exec(compile('def mk(k):\n    def f(x):\n        return x * k\n    return f\n', 'ff.py', 'exec'),
         other.__dict__)
    monkeypatch.setitem(sys.modules, 'fake_factory', other)
    fa = ec.code_fingerprint(_ns(_S=other.mk(3), mk=other.mk))
    fb = ec.code_fingerprint(_ns(_S=other.mk(4), mk=other.mk))
    assert None not in (fa, fb) and fa != fb
    assert ec.code_fingerprint(_ns(mk=other.mk)) == ec.code_fingerprint(_ns())  # the import itself: its own module's


def test_the_qmax_fingerprint_covers_how_bmssample_derives_its_inputs_and_nothing_else_of_bms_py(tmp_path):
    """Rev8: the SoC replacement in bms.py was outside it. Only that function
    counts: an unrelated edit of bms.py keeps months of segments."""
    pkg = _copy_bmslib(tmp_path)
    src = pkg / 'bms.py'
    text = src.read_text()
    probe = 'import bmslib.qmax as m; print(m.CODE_FINGERPRINT)'
    base, = _py(tmp_path, probe, '-B')
    for old, new, same in (('elif math.isnan(capacity) and soc > .2', 'elif math.isnan(capacity) and soc > .3', False),
                           ('MIN_VALUE_EXPIRY = 20', 'MIN_VALUE_EXPIRY = 21', True)):
        assert text.count(old) == 1
        src.write_text(text.replace(old, new))
        edited, = _py(tmp_path, probe, '-B')
        assert base != 'None' and edited != 'None' and (edited == base) == same, old
    src.write_text(text)


def test_a_class_nested_deeper_than_the_walk_leaves_the_fingerprint_unknown():
    """It used to be skipped: a constant in it went unseen (rev8)."""
    src = 'class _A:\n class _B:\n  class _C:\n   class _D:\n    class _E:\n     LIM = {V}\n'
    assert _fp_src(src.format(V='1.0')) is None
    assert _fp_src('class _A:\n class _B:\n  class _C:\n   LIM = 1.0\n') is not None


def test_a_wrapped_function_is_not_configured_by_its_wrapper_attribute():
    """functools.wraps sets __wrapped__: that is the wrapped code, walked on
    its own, not a value."""
    src = ('import functools as _ft\ndef _deco(f):\n    @_ft.wraps(f)\n    def w(*a):\n        return f(*a)\n    return w\n'
           '@_deco\ndef _g(v):\n    return v + {V}\n')
    fa, fb = _fp_src(src.format(V='1')), _fp_src(src.format(V='2'))
    assert None not in (fa, fb) and fa != fb


def test_unknown_closure_contents_leave_the_fingerprint_unknown():
    assert _fp_src('_O = object()\ndef _mk(o):\n    def f(x):\n        return o\n    return f\n_F = _mk(_O)\n'
                   'del _O\n') is None
    assert _fp_src('class _E:\n    _CFG = {{"x": object()}}\n'.format()) is None
    assert _fp_src('_CFG = [object()]\n') is None


def test_a_recursive_closure_is_fingerprinted():
    src = 'def _mk(k):\n    def f(x):\n        return f(x - 1) if x > k else x\n    return f\n_F = _mk({V})\n'
    fa, fb = _fp_src(src.format(V=1)), _fp_src(src.format(V=2))
    assert None not in (fa, fb) and fa != fb


def test_state_names_must_be_a_frozenset_of_names():
    assert ec.code_fingerprint(_ns(K=1, _FINGERPRINT_STATE=['K'])) is None
    assert ec.code_fingerprint(_ns(K=1, C={}, _FINGERPRINT_STATE=frozenset({'C'}))) == \
        ec.code_fingerprint(_ns(K=1, C={'filled': 1}, _FINGERPRINT_STATE=frozenset({'C'})))


def _ns(**kw):
    ns = {'__name__': 'fake_mod'}
    ns.update(kw)
    return ns


def _fn(src, name='f'):
    ns = {'__name__': 'fake_mod'}
    exec(compile(src, 'fake.py', 'exec'), ns)
    return ns[name]


def test_code_and_constants_count_and_the_logger_does_not():
    import logging
    base = ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.5))
    assert base is not None
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 2\n'), K=1.5)) != base
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.6)) != base
    assert ec.code_fingerprint(_ns(f=_fn('def f(x=3):\n    return x + 1\n'), K=1.5)) != base  # defaults
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.5,
                                   log=logging.getLogger('t'))) == base


def test_a_function_docstring_is_not_code():
    """A function's docstring is its code object's first constant (on 3.14
    flagged as such); it is masked, or rewording one would discard months of
    saved segments. The class docstring is a separate thing (__doc__)."""
    a = _fn('def f(x):\n    """One thing."""\n    return x + 1\n')
    b = _fn('def f(x):\n    """Another thing,\n    over two lines."""\n    return x + 1\n')
    c = _fn('def f(x):\n    """One thing."""\n    return x + 2\n')
    fa, fb, fc = (ec.code_fingerprint(_ns(f=g)) for g in (a, b, c))
    assert fa == fb and fa is not None and fc != fa
    # a string constant that is not the docstring still counts
    d = _fn('def f(x):\n    y = "one"\n    return x + 1\n')
    e = _fn('def f(x):\n    y = "two"\n    return x + 1\n')
    assert ec.code_fingerprint(_ns(f=d)) != ec.code_fingerprint(_ns(f=e))


def test_the_python_version_is_part_of_the_fingerprint(monkeypatch):
    """The same bytecode is not the same program on another interpreter. A
    namespace of constants only has no bytecode that would differ, so the
    version term alone separates them."""
    import types as t
    base = ec.code_fingerprint(_ns(K=1.5, f=_fn('def f(x):\n    return x + 1\n')))
    consts = ec.code_fingerprint(_ns(K=1.5))
    monkeypatch.setattr(ec, 'sys', t.SimpleNamespace(version_info=(3, 99, 0, 'final', 0)))
    assert ec.code_fingerprint(_ns(K=1.5)) not in (consts, None)
    assert ec.code_fingerprint(_ns(K=1.5, f=_fn('def f(x):\n    return x + 1\n'))) not in (base, None)


def test_a_locked_method_is_fingerprinted_by_its_body():
    """@locked replaces a method with a generic wrapper; the body lives in
    __wrapped__ and must count, or every change to add() would go unseen."""
    def cls(body):
        ns = {'__name__': 'fake_mod', 'locked': ec.locked}
        exec(compile('class E:\n    @locked\n    def add(self, x):\n        return x + %d\n' % body, 'fake.py', 'exec'),
             ns)
        return _ns(E=ns['E'])
    assert ec.code_fingerprint(cls(1)) == ec.code_fingerprint(cls(1))
    assert ec.code_fingerprint(cls(1)) != ec.code_fingerprint(cls(2))


def test_what_cannot_be_fingerprinted_is_unknown_never_a_match():
    f = _fn('def f(x):\n    return x\n')
    f.__defaults__ = (object(),)  # not marshallable
    assert ec.code_fingerprint(_ns(f=f)) is None
    assert ec.code_fingerprint({'no': 'name'}) is None


def test_the_estimators_fingerprint_their_code_and_the_shared_helpers():
    import bmslib.impedance as imp
    import bmslib.qmax as q
    assert q.CODE_FINGERPRINT and imp.CODE_FINGERPRINT and q.CODE_FINGERPRINT != imp.CODE_FINGERPRINT
    import bmslib.bms as bms
    inputs = {'__name__': 'bmslib.bms', 'derive_charge_fields': bms.derive_charge_fields}
    assert q.CODE_FINGERPRINT == q.code_fingerprint() == ec.code_fingerprint(vars(q), vars(ec), inputs)
    assert ec.code_fingerprint(vars(q), inputs) != q.CODE_FINGERPRINT  # the shared helpers are in it
    assert ec.code_fingerprint(vars(q), vars(ec)) != q.CODE_FINGERPRINT  # and BmsSample's derivation


# ---------------------------------------------------------------- configured objects (third review)

def _curve_fp(monkeypatch, curve):
    import bmslib.qmax as q
    monkeypatch.setattr(q, 'DEFAULT_CURVE', curve)
    return q.code_fingerprint()


CURVE_MUTATIONS = [
    ('arguments', lambda q: q.OcvCurve(sigma=1.0, min_slope=1.0)),
    ('slope gate', lambda q: q.OcvCurve(min_slope=1.0)),
    ('smoothing sigma', lambda q: q.OcvCurve(sigma=2.0)),
    ('data', lambda q: q.OcvCurve([v + 20.0 for v in q.OCV_RAW_MV])),
    ('one value', lambda q: q.OcvCurve((3326.29, 3326.28) + q.OCV_RAW_MV[2:])),
]


@pytest.mark.parametrize('what,make', CURVE_MUTATIONS, ids=[m[0] for m in CURVE_MUTATIONS])
def test_the_curve_the_module_runs_with_is_fingerprinted(monkeypatch, what, make):
    """qmax.DEFAULT_CURVE = OcvCurve(): its arguments are not code and were
    left out (third review), so OcvCurve(sigma=1.0, min_slope=1.0) or a
    shifted table kept the fingerprint and old anchors restored under another
    curve. The same curve built again keeps it."""
    import bmslib.qmax as q
    assert _curve_fp(monkeypatch, q.OcvCurve()) == q.CODE_FINGERPRINT
    assert _curve_fp(monkeypatch, make(q)) not in (q.CODE_FINGERPRINT, None)


def test_calibration_a_curve_that_describes_nothing_keeps_the_fingerprint(monkeypatch):
    """Known-bad for the object rule: without what the curve says about
    itself, a curve with another smoothing and slope gate fingerprints as the
    shipped one -- the review's measurement at cab71f4."""
    import bmslib.qmax as q
    monkeypatch.setattr(q.OcvCurve, 'fingerprint_data', lambda self: ())
    base = _curve_fp(monkeypatch, q.OcvCurve())
    assert _curve_fp(monkeypatch, q.OcvCurve(sigma=1.0, min_slope=1.0)) == base
    assert _curve_fp(monkeypatch, q.OcvCurve([v + 20.0 for v in q.OCV_RAW_MV])) == base


@pytest.mark.parametrize('new', ['DEFAULT_CURVE = OcvCurve(sigma=1.0, min_slope=1.0)',
                                 'DEFAULT_CURVE = OcvCurve([v + 20.0 for v in OCV_RAW_MV])'])
def test_an_edited_default_curve_changes_the_fingerprint_of_the_real_module(tmp_path, new):
    """The same, as an edit of qmax.py in a fresh process (the review's
    mutation)."""
    pkg = _copy_bmslib(tmp_path)
    src = pkg / 'qmax.py'
    text = src.read_text()
    old = 'DEFAULT_CURVE = OcvCurve()'
    assert text.count(old) == 1
    probe = 'import bmslib.qmax as m; print(m.CODE_FINGERPRINT)'
    base, = _py(tmp_path, probe, '-B')
    src.write_text(text.replace(old, new))
    edited, = _py(tmp_path, probe, '-B')
    assert base != 'None' and edited not in (base, 'None')


def test_an_object_that_cannot_describe_itself_leaves_the_fingerprint_unknown():
    """Any module-level object might configure behaviour. The logger and type
    annotations are known to configure nothing; anything else without a
    fingerprint_data() makes the fingerprint None, so state is never restored
    into code that cannot be vouched for."""
    import logging
    from typing import Optional

    class Configured:
        def __init__(self, k):
            self.k = k

        def fingerprint_data(self):
            return (self.k,)

    import bisect
    import math
    base = ec.code_fingerprint(_ns(K=1.5))
    assert ec.code_fingerprint(_ns(K=1.5, log=logging.getLogger('x'), Opt=Optional)) == base
    # an imported C function is imported code, as an imported Python function is (the review's util_import)
    assert ec.code_fingerprint(_ns(K=1.5, _flo=math.floor, _bis=bisect.bisect_left)) == base
    assert ec.code_fingerprint(_ns(K=1.5, cfg=object())) is None
    assert ec.code_fingerprint(_ns(K=1.5, add=[].append)) is None  # bound to state
    a, b = ec.code_fingerprint(_ns(K=1.5, cfg=Configured(1))), ec.code_fingerprint(_ns(K=1.5, cfg=Configured(2)))
    assert None not in (a, b) and len({a, b, base}) == 3
