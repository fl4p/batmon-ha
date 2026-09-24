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
    for f in ('__init__.py', 'estimator_common.py', 'qmax.py', 'impedance.py', 'util.py'):
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
    assert text.count(cls) == 1 and text.count(doc) == 1 and text.count(meth) == 1
    cosmetic = (text.replace(anchor, anchor + '# a comment\n\n\n# and another\n')
                .replace(cls, '# a comment above the class\n' + cls)
                .replace(doc, '"""Streaming estimator, per BMS. Feed add()\n    once per sampler iteration."""')
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
    constant, so the fingerprint depended on when it was computed."""
    imp = _own_lag_cache(monkeypatch)
    before = ec.code_fingerprint(vars(imp), vars(ec))
    imp._best_lag_diffs([0.1] * 30, [0.2] * 30, max_lag=7)  # a new cache entry
    assert 7 in imp._LAG_ORDER_CACHE  # precondition: it is state, and it changed
    assert ec.code_fingerprint(vars(imp), vars(ec)) == before == imp.CODE_FINGERPRINT


def test_calibration_a_mutable_container_counted_as_a_constant_follows_the_state(monkeypatch):
    imp = _own_lag_cache(monkeypatch)
    orig = ec._is_const
    monkeypatch.setattr(ec, '_is_const', lambda x: isinstance(x, dict) or orig(x))
    before = ec.code_fingerprint(vars(imp), vars(ec))
    assert before is not None
    imp._best_lag_diffs([0.1] * 30, [0.2] * 30, max_lag=7)
    assert ec.code_fingerprint(vars(imp), vars(ec)) != before


def test_the_estimators_keep_no_configuration_in_mutable_containers():
    """Module-level lists, dicts and sets are left out of the fingerprint as
    state. So configuration must not live in one, or a change to it would
    restore state computed with the old value. A new one fails here and needs
    a decision: a tuple, or state (then name it here)."""
    import bmslib.impedance as imp
    import bmslib.qmax as q
    found = {m.__name__ + '.' + k for m in (q, imp, ec) for k, v in vars(m).items()
             if not k.startswith('__') and isinstance(v, (list, dict, set, bytearray))}
    assert found == {'bmslib.impedance._LAG_ORDER_CACHE'}


def _ns(**kw):
    ns = {'__name__': 'fake_mod'}
    ns.update(kw)
    return ns


def _fn(src, name='f'):
    ns = {'__name__': 'fake_mod'}
    exec(compile(src, 'fake.py', 'exec'), ns)
    return ns[name]


def test_code_and_constants_count_and_other_objects_do_not():
    base = ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.5))
    assert base is not None
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 2\n'), K=1.5)) != base
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.6)) != base
    assert ec.code_fingerprint(_ns(f=_fn('def f(x=3):\n    return x + 1\n'), K=1.5)) != base  # defaults
    assert ec.code_fingerprint(_ns(f=_fn('def f(x):\n    return x + 1\n'), K=1.5, log=object())) == base


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
    assert q.CODE_FINGERPRINT == ec.code_fingerprint(vars(q), vars(ec))
    assert ec.code_fingerprint(vars(q)) != q.CODE_FINGERPRINT  # the shared helpers are in it
