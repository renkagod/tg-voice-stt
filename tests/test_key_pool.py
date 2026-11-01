import os
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

# Point DB_PATH to a temporary file for tests
temp_dir = tempfile.mkdtemp()
test_db_path = os.path.join(temp_dir, "test_keys.db")
os.environ["DB_PATH"] = test_db_path

import key_pool

class TestKeyPool(unittest.TestCase):
    def setUp(self):
        with key_pool.get_db() as conn:
            conn.execute("DROP TABLE IF EXISTS api_keys")
            conn.commit()
        key_pool.init_pool(default_env_key="AIzaSySYSTEM_KEY_TEST_001")

    def tearDown(self):
        with key_pool.get_db() as conn:
            conn.execute("DROP TABLE IF EXISTS api_keys")
            conn.commit()

    def test_init_seeds_system_key(self):
        status = key_pool.get_pool_status_for_user(0)
        self.assertEqual(status["total_active"], 1)
        self.assertEqual(status["available"], 1)
        key = key_pool.get_active_key()
        self.assertEqual(key, "AIzaSySYSTEM_KEY_TEST_001")

    @patch("key_pool.validate_key_online", new_callable=AsyncMock)
    def test_add_key_duplicate_prevention(self, mock_validate):
        mock_validate.return_value = (True, "OK")
        import asyncio

        # Add key for user 111
        success, msg = asyncio.run(key_pool.add_user_key(111, "AIzaSyUSER_KEY_A"))
        self.assertTrue(success)

        # Try adding the exact same key for user 222
        success2, msg2 = asyncio.run(key_pool.add_user_key(222, "AIzaSyUSER_KEY_A"))
        self.assertFalse(success2)
        self.assertEqual(msg2, "duplicate")

        # The owner re-sending an active key of their own
        success_mine, msg_mine = asyncio.run(key_pool.add_user_key(111, "AIzaSyUSER_KEY_A"))
        self.assertFalse(success_mine)
        self.assertEqual(msg_mine, "mine")

        # Try adding system key
        success3, msg3 = asyncio.run(key_pool.add_user_key(111, "AIzaSySYSTEM_KEY_TEST_001"))
        self.assertFalse(success3)
        self.assertEqual(msg3, "duplicate")

    @patch("key_pool.validate_key_online", new_callable=AsyncMock)
    def test_round_robin_rotation(self, mock_validate):
        mock_validate.return_value = (True, "OK")
        import asyncio

        asyncio.run(key_pool.add_user_key(10, "KEY_1"))
        asyncio.run(key_pool.add_user_key(20, "KEY_2"))

        # We have 3 keys now: SYSTEM_KEY, KEY_1, KEY_2
        picked = [key_pool.get_active_key() for _ in range(3)]
        self.assertEqual(set(picked), {"AIzaSySYSTEM_KEY_TEST_001", "KEY_1", "KEY_2"})

        # Next 3 should also rotate
        picked_again = [key_pool.get_active_key() for _ in range(3)]
        self.assertEqual(len(picked_again), 3)

    def test_cooldown_behavior(self):
        key = key_pool.get_active_key()
        self.assertEqual(key, "AIzaSySYSTEM_KEY_TEST_001")

        # Put key on cooldown for 10 seconds
        key_pool.put_on_cooldown(key, seconds=10)

        # Now no keys available
        self.assertIsNone(key_pool.get_active_key())

        status = key_pool.get_pool_status_for_user(0)
        self.assertEqual(status["total_active"], 1)
        self.assertEqual(status["on_cooldown"], 1)
        self.assertEqual(status["available"], 0)

        # Force expire cooldown
        with key_pool.get_db() as conn:
            conn.execute("UPDATE api_keys SET cooldown_until = 0 WHERE api_key = ?", (key,))
            conn.commit()

        # Should be available again
        self.assertEqual(key_pool.get_active_key(), "AIzaSySYSTEM_KEY_TEST_001")

    def test_revoke_key_and_owner_notification(self):
        with key_pool.get_db() as conn:
            conn.execute(
                "INSERT INTO api_keys (user_id, api_key, is_active) VALUES (999, 'REVOKE_ME', 1)"
            )
            conn.commit()

        owner_id = key_pool.revoke_key("REVOKE_ME", reason="403 Forbidden")
        self.assertEqual(owner_id, 999)

        # Should not be returned in get_active_key
        # Only system key left
        self.assertEqual(key_pool.get_active_key(), "AIzaSySYSTEM_KEY_TEST_001")

        # Check access
        self.assertFalse(key_pool.has_access(999, admin_ids={123}))

    def test_revoke_user_keys_command(self):
        with key_pool.get_db() as conn:
            conn.execute("INSERT INTO api_keys (user_id, api_key, is_active) VALUES (555, 'K1', 1)")
            conn.execute("INSERT INTO api_keys (user_id, api_key, is_active) VALUES (555, 'K2', 1)")
            conn.commit()

        self.assertTrue(key_pool.has_access(555, admin_ids=set()))
        count = key_pool.revoke_user_keys(555)
        self.assertEqual(count, 2)
        self.assertFalse(key_pool.has_access(555, admin_ids=set()))

    def test_revoke_and_restore_single_key(self):
        with key_pool.get_db() as conn:
            conn.execute("INSERT INTO api_keys (user_id, api_key, is_active) VALUES (555, 'K1', 1)")
            conn.execute("INSERT INTO api_keys (user_id, api_key, is_active) VALUES (555, 'K2', 1)")
            conn.commit()
        keys = key_pool.get_pool_status_for_user(555)["user_keys"]
        k1, k2 = keys[0]["id"], keys[1]["id"]

        # Someone else cannot touch the key
        self.assertFalse(key_pool.revoke_user_key(777, k1))

        self.assertTrue(key_pool.revoke_user_key(555, k1))
        self.assertFalse(key_pool.revoke_user_key(555, k1))
        self.assertTrue(key_pool.has_access(555, admin_ids=set()))
        states = {k["id"]: (k["state"], k["revoked_by"]) for k in key_pool.get_pool_status_for_user(555)["user_keys"]}
        self.assertEqual(states[k1], ("revoked", "user"))
        self.assertEqual(states[k2], ("active", ""))

        self.assertTrue(key_pool.restore_user_key(555, k1))
        self.assertEqual(key_pool.get_pool_status_for_user(555)["user_keys"][0]["state"], "active")

    def test_google_rejected_key_cannot_be_restored(self):
        with key_pool.get_db() as conn:
            conn.execute("INSERT INTO api_keys (user_id, api_key, is_active) VALUES (555, 'DEAD', 1)")
            conn.commit()
        key_pool.revoke_key("DEAD", reason="403")
        key = key_pool.get_pool_status_for_user(555)["user_keys"][0]
        self.assertEqual(key["revoked_by"], "google")
        self.assertFalse(key_pool.restore_user_key(555, key["id"]))

    @patch("key_pool.validate_key_online", new_callable=AsyncMock)
    def test_resending_own_revoked_key_restores_it(self, mock_validate):
        mock_validate.return_value = (True, "OK")
        import asyncio
        asyncio.run(key_pool.add_user_key(111, "AIzaSyUSER_KEY_B"))
        key_pool.revoke_user_keys(111)
        success, code = asyncio.run(key_pool.add_user_key(111, "AIzaSyUSER_KEY_B"))
        self.assertTrue(success)
        self.assertEqual(code, "restored")
        self.assertTrue(key_pool.has_access(111, admin_ids=set()))

    def test_has_access_for_admins(self):
        # Admin has access even without keys in DB
        self.assertTrue(key_pool.has_access(777, admin_ids={777, 888}))
        self.assertFalse(key_pool.has_access(999, admin_ids={777, 888}))

    def test_mask_key(self):
        self.assertEqual(key_pool.mask_key("AIzaSyDophAQuyyiBr8h0nypEwXUKozH-BEswD0"), "AIza...swD0")
        self.assertEqual(key_pool.mask_key("short"), "****")

if __name__ == "__main__":
    unittest.main()
