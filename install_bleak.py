import subprocess
import sys

from bmslib.store import load_user_config

user_config = load_user_config()

if user_config.get('install_newer_bleak'):
    args = ['install', 'bleak==0.20.2']
else:
    args = ['install', 'git+https://github.com/jpeters-ml/bleak@feature/windowsPairing']

print('running pip3 ' + ' '.join(args))

p = subprocess.Popen(["pip3"] + args, stdout=sys.stdout, stderr=sys.stderr) #, stdout=subprocess.PIPE)
p.wait(timeout=90)

sys.exit(p.returncode)
