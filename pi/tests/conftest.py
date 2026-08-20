import os
import sys
import tempfile

# pi/ has no __init__.py — put it on sys.path so `import recording_engine`
# and `import web_recorder` work regardless of cwd pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# recording_engine.py creates REC_DIR at import time (os.makedirs). Point it
# at a throwaway dir before any test module imports it, so test runs don't
# create ~/recordings on the dev machine.
os.environ.setdefault("REC_DIR", tempfile.mkdtemp(prefix="rpi5-recorder-tests-"))
