"""Add the Muse route, validate Caddy, and reload without stopping English apps."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

path = Path('/etc/caddy/Caddyfile')
previous = path.read_text()
marker = '    handle {\n'
route = '''    redir /muse /muse/ 308
    handle_path /muse/* {
        @managed_update path /admin/update/* /admin/repo/*
        header @managed_update Content-Type application/json
        respond @managed_update `{"detail":"Updates are managed through server deployment."}` 403
        request_body {
            max_size 16777216
        }
        reverse_proxy 127.0.0.1:18610 {
            transport http {
                dial_timeout 5s
                response_header_timeout 660s
            }
        }
    }
'''
if 'handle_path /muse/*' not in previous:
    assert previous.count(marker) == 1, 'Unexpected Caddy layout; inspect before editing'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = path.with_name('Caddyfile.before-muse2api-' + stamp)
    shutil.copy2(path, backup)
    path.write_text(previous.replace(marker, route + marker))
    try:
        subprocess.run(['caddy', 'validate', '--config', str(path)], check=True)
        subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
    except Exception:
        shutil.copy2(backup, path)
        subprocess.run(['systemctl', 'reload', 'caddy'], check=True)
        raise
    print('Muse route enabled; previous Caddy config:', backup)
else:
    print('Muse route already present; left unchanged')
