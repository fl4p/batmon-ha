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


def test_calibration_marshal_alone_depends_on_the_hash_seed():
    """Known-bad for the set sorting: on Python 3.10, which the Dockerfile
    still accepts, marshal writes a set of strings in hash order, so its bytes
    change with the seed (measured 2026-09-24: 3.10 differs, 3.12-3.14 sort).
    Where this interpreter sorts, there is nothing to calibrate against."""
    code = "import marshal; print(marshal.dumps(frozenset(['alpha', 'beta', 'gamma', 'delta', 'epsilon'])).hex())"
    outs = set()
    for seed in ('1', '2', '3', '4', '5', '6'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.add(subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                                timeout=60).stdout)
    if len(outs) == 1:
        pytest.skip('marshal sorts sets on Python %d.%d' % sys.version_info[:2])
    assert len(outs) > 1


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
