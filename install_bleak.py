import subprocess
import sys

from bmslib.bt import bleak_version
from bmslib.store import load_user_config
from bmslib.util import get_logger

user_config = load_user_config()
logger = get_logger()

if user_config.get('install_newer_bleak'):
    ver = '0.20.2'
    args = ['install', 'bleak==0.20.2']
else:
    ver = '0.13.1a1'
    args = ['install', 'git+https://github.com/jpeters-ml/bleak@feature/windowsPairing']

installed_ver = bleak_version()
if installed_ver == ver:
    sys.exit(0)

logger.info(f'bleak {installed_ver} installed, want {ver}, running pip3 ' + ' '.join(args))

p = subprocess.Popen(["pip3"] + args, stdout=sys.stdout, stderr=sys.stderr)  # , stdout=subprocess.PIPE)
p.wait(timeout=120)

sys.exit(p.returncode)
