r"""Write this folder's real location into web.config. Run it once on the server, from this folder:

    .venv\Scripts\python set_server_path.py

IIS needs scriptProcessor as a fixed path that matches what wfastcgi-enable registers, so it is the
one line of web.config that cannot follow the folder by itself. This fills it in wherever the app
was placed, and also creates the logs folder so its permissions can be set.
"""
import os
import re
import sys
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_CONFIG = os.path.join(ROOT, 'web.config')
SCRIPT_PROCESSOR = re.compile(r'scriptProcessor="[^"]*"')


def script_processor(python, wfastcgi):
    """The value wfastcgi-enable registers, which quotes any path containing a space."""
    return '|'.join(f'"{path}"' if ' ' in path else path for path in (python, wfastcgi))


def update_web_config(path, value):
    with open(path, encoding='utf-8', newline='') as file:
        text = file.read()
    attribute = 'scriptProcessor="' + escape(value, {'"': '&quot;'}) + '"'
    text, count = SCRIPT_PROCESSOR.subn(lambda match: attribute, text)
    if count != 1:
        raise SystemExit(f'Expected one scriptProcessor in {path}, found {count}.')
    with open(path, 'w', encoding='utf-8', newline='') as file:
        file.write(text)


def main():
    if os.path.normcase(sys.prefix) != os.path.normcase(os.path.join(ROOT, '.venv')):
        raise SystemExit(r"Run this with the app's own Python: .venv\Scripts\python set_server_path.py")

    import wfastcgi

    value = script_processor(sys.executable, wfastcgi.__file__)
    update_web_config(WEB_CONFIG, value)
    os.makedirs(os.path.join(ROOT, 'logs'), exist_ok=True)
    print('web.config now uses:')
    print(value)
    print('wfastcgi-enable must show these same paths.')


if __name__ == '__main__':
    main()
