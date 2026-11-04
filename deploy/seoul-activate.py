"""Run as root after code/dependencies are installed; never prints credentials."""
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess

home = Path('/var/lib/muse2api')
env = home / '.env'
owner = pwd.getpwnam('muse2api')
if not env.exists():
    values = {
        'MUSE2API_HOME': str(home),
        'MUSE2API_HOST': '127.0.0.1',
        'MUSE2API_PORT': '18610',
        'MUSE2API_CHROMIUM': '/usr/bin/google-chrome-stable',
        'MUSE2API_CDP_PORT': '19210',
        'MUSE2API_HOME_DIR': str(home),
        'MUSE2API_PROFILE_ROOT': str(home / 'profiles'),
        'MUSE2API_CONCURRENCY': '1',
        'MUSE2API_KEY': 'm2a_' + secrets.token_hex(32),
        'MUSE2API_PUBLIC_BASE': 'https://alexych.asia/muse',
        'MUSE2API_REF_IMAGE_LOCAL_FILES': '0',
        'MUSE2API_REF_IMAGE_PRIVATE_FETCH': '0',
        'MUSE2API_KEEPALIVE_DISABLED_ACCOUNTS': '0',
    }
    fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    os.chown(env, owner.pw_uid, owner.pw_gid)
shutil.copyfile('/opt/muse2api/deploy/seoul.service', '/etc/systemd/system/muse2api.service')
subprocess.run(['systemd-analyze', 'verify', '/etc/systemd/system/muse2api.service'], check=True)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'muse2api'], check=True)
print('Muse2API service installed with private credentials.')
