"""Browser/CDP smoke test without logging into Muse or running generation."""
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/opt/muse2api')
from config import CFG
from engine import MuseEngine

engine = MuseEngine(CFG)
try:
    engine.start()
    version = engine.browser.send('Browser.getVersion')['result']
    context = engine.browser.send('Target.createBrowserContext', {})['result']['browserContextId']
    page = engine.browser.send('Target.createTarget', {
        'url': 'about:blank', 'browserContextId': context})['result']['targetId']
    target = engine.browser.send('Target.getTargetInfo', {'targetId': page})['result']
    engine.browser.send('Target.disposeBrowserContext', {'browserContextId': context})
    print(json.dumps({'browser': version.get('product'),
                      'isolated_page': target['targetInfo']['type'],
                      'generation_invoked': False}))
    print(subprocess.check_output(['ss', '-lntp', 'sport', '=', ':19210'], text=True))
    cgroup = next(line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
    print('browser_cgroup=' + cgroup)
    print('memory_current=' + (Path('/sys/fs/cgroup') / cgroup.lstrip('/') / 'memory.current').read_text().strip())
finally:
    engine.stop()
