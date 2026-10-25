"""Run tests that never start a browser or contact a platform."""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
NAMES = ['test_cli', 'test_jobs', 'test_media', 'test_portable', 'test_topics.TopicRankingTests', 'test_browser.CollectionTests', 'test_douyin_handoff']

if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("offline tests cannot use the network")):
        suite = unittest.defaultTestLoader.loadTestsFromNames(NAMES)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
