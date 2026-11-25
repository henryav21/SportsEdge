"""Offline test for sharing the dashboard: the outside address is an extra, and failing to set it up must not
take the dashboard on this computer down.
Run:  python -m unittest discover tests"""
import os
import tempfile
import unittest
from unittest import mock

from sportsedge import share


class SharingFails(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = os.environ.get("SPORTSEDGE_HISTORY")
        os.environ["SPORTSEDGE_HISTORY"] = self.dir.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("SPORTSEDGE_HISTORY", None)
        else:
            os.environ["SPORTSEDGE_HISTORY"] = self.old
        self.dir.cleanup()

    def run_share(self, deploys):
        """Run the share loop with the tunnel and the deploy stubbed; `deploys` is what each deploy attempt does."""
        tunnel = mock.Mock()
        tunnel.wait.return_value = "https://x.trycloudflare.com"
        tunnel.alive.return_value = False
        with mock.patch.object(share, "_local_ok", return_value=True), mock.patch.object(share, "_gate_ok", return_value=True), \
                mock.patch.object(share, "_keep_awake"), mock.patch.object(share.access, "has_passcode", return_value=True), \
                mock.patch.object(share, "Tunnel", return_value=tunnel), mock.patch.object(share, "point_vercel_at", side_effect=deploys), \
                mock.patch.object(share.time, "sleep") as sleep, mock.patch("builtins.print"):
            share.run(8765)  # must come back by itself: a SystemExit here would have ended the dashboard too
        return tunnel, sleep

    def test_a_refused_deploy_is_tried_again_instead_of_ending_everything(self):
        tunnel, sleep = self.run_share([RuntimeError("The Vercel deploy failed"), RuntimeError("again"), KeyboardInterrupt()])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [share.RETRY_SECONDS, share.RETRY_SECONDS])
        self.assertGreaterEqual(tunnel.stop.call_count, 2)  # the half-open tunnel is closed before each new try
        self.assertFalse((share.tracker.history_dir() / "share.json").exists())  # no address is shown while there is none

    def test_a_working_share_writes_the_address_for_the_dashboard(self):
        seen = {}

        def deploy(url):
            if seen:
                raise KeyboardInterrupt()
            seen["url"] = url
            return "https://example.vercel.app"

        beats = []
        with mock.patch.object(share, "_beat", side_effect=lambda u, t: beats.append((u, t))):
            self.run_share(deploy)
        self.assertEqual(beats, [("https://example.vercel.app", "https://x.trycloudflare.com")])


if __name__ == "__main__":
    unittest.main()
