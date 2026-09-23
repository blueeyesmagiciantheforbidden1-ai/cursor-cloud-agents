"""Local test entrypoint; no provider or Google network calls."""
from pathlib import Path
import sys
import unittest

SOURCE = Path('C:/Users/9/.codex/visualizations/2026/09/20/01a0bfe3-8100-7811-8e7f-992bfc4740b3/agent-hub')
sys.path.insert(0, str(SOURCE))
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or ['test_live_loop', 'test_dynamic_broker'])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
