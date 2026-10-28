"""Run in a local terminal; never paste the password into chat."""
import getpass
import os
from pathlib import Path

path = Path(__file__).resolve().parent.parent / 'config/local/mysql.properties'
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
username = input('Test MySQL username: ').strip()
password = getpass.getpass('Test MySQL password (hidden): ')
if not username or not password:
    raise SystemExit('Username and password must not be empty.')
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    def escape(value):
        return ''.join('\\u%04x' % ord(c) for c in value)
    output.write('test.mysql.username=' + escape(username) + '\n')
    output.write('test.mysql.password=' + escape(password) + '\n')
print('Saved test credentials locally with mode 600. No connection has been made.')
