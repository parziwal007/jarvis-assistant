import os
import unittest
from unittest.mock import patch

import main
from automation import security


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.hash = security.make_hash("iron man sucks")
        env = patch.dict(os.environ, {"JARVIS_PASSWORD_HASH": self.hash, "JARVIS_LOCK_AFTER_MINUTES": "15"})
        env.start()
        self.addCleanup(env.stop)
        security._reset_for_tests()
        self.addCleanup(security._reset_for_tests)
        main._PENDING_ACTION = None

    def test_hash_never_contains_the_password(self):
        self.assertNotIn("iron", self.hash)
        self.assertTrue(self.hash.startswith("pbkdf2_sha256$"))

    def test_accepts_the_ways_whisper_writes_it(self):
        for said in ["iron man sucks", "Iron Man sucks.", "Ironman sucks!", "Jarvis, iron man sucks",
                     "the password is iron man sucks", "IRON-MAN SUCKS"]:
            self.assertTrue(security.matches(said), said)

    def test_rejects_other_phrases(self):
        for said in ["iron man", "iron man rocks", "open youtube", ""]:
            self.assertFalse(security.matches(said), said)

    def test_locked_jarvis_does_nothing_until_password(self):
        with patch.object(main.agent, "handle") as agent, patch.object(main.app, "trigger_action") as action:
            self.assertEqual(main.handle_command("WAKE_SIGNAL_CONFIRMED", "voice"),
                             ("I'm locked. What's the password?", False))
            reply, _ = main.handle_command("open whatsapp", "voice")
            self.assertEqual(reply, "That's not the password. Please try again.")
            agent.assert_not_called()
            action.assert_not_called()
        reply, _ = main.handle_command("Iron Man sucks.", "voice")
        self.assertEqual(reply, "Password accepted. Welcome back, Sarthak.")
        self.assertFalse(security.is_locked())

    def test_lock_command_locks_again(self):
        main.handle_command("iron man sucks", "typed")
        self.assertEqual(main.handle_command("lock yourself", "voice")[0],
                         "Locked. Say the password when you need me again.")
        self.assertTrue(security.is_locked())

    def test_locks_itself_after_idle_time(self):
        main.handle_command("iron man sucks", "typed")
        security._state["last_used"] -= 16 * 60
        self.assertTrue(security.is_locked())

    def test_too_many_wrong_tries_blocks_for_a_while(self):
        for _ in range(security.MAX_TRIES):
            main.handle_command("wrong guess", "voice")
        reply, _ = main.handle_command("iron man sucks", "voice")
        self.assertIn("Too many wrong tries", reply)
        self.assertTrue(security.is_locked())

    def test_notifications_are_held_until_unlocked(self):
        security.hold_message("Important email from Prof. Rao.")
        reply, _ = main.handle_command("iron man sucks", "voice")
        self.assertIn("While I was locked: Important email from Prof. Rao.", reply)

    def test_password_is_hidden_in_logs_while_locked(self):
        self.assertEqual(security.hide("iron man sucks"), "(hidden while Jarvis is locked)")
        security.try_unlock("iron man sucks")
        self.assertEqual(security.hide("open youtube"), "open youtube")

    def test_no_password_set_means_no_lock(self):
        with patch.dict(os.environ, {"JARVIS_PASSWORD_HASH": ""}):
            self.assertFalse(security.is_locked())


if __name__ == "__main__":
    unittest.main()
