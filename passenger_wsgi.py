import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(PROJECT_ROOT, "_vendor")
PUBLIC_VENDOR_DIR = os.path.join(PROJECT_ROOT, "public_html", "_vendor")

for path in (PROJECT_ROOT, VENDOR_DIR, PUBLIC_VENDOR_DIR):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app import app as application

application.debug = False
