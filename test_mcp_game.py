from __future__ import annotations

import unittest
from unittest.mock import patch

from hide_seek import ADJ, BREATH_MAX, ROOM_SPOTS
from mcp_game import _decode_state, game_guide, start_game, take_turn


class McpGameTests(unittest.TestCase):
    def test_guide_exposes_rooms_spots_and_actions(self):
        guide = game_guide()
        self.assertIn("主卧", guide["hideable_rooms"])
        self.assertIn("床底", guide["spots"]["主卧"])
        self.assertIn("屏息", guide["actions"])

    def test_start_returns_restorable_token(self):
        with patch("mcp_game.random.choice", return_value="沙发后"):
            result = start_game("主卧", "床底")
        game, belief, searched = _decode_state(result["state_token"])
        self.assertEqual(game.her_room, "主卧")
        self.assertEqual(game.her_spot, "床底")
        self.assertEqual(result["status"], "running")
        self.assertTrue(belief.probs)
        self.assertIsInstance(searched, dict)

    def test_start_rejects_transit_room(self):
        with self.assertRaisesRegex(ValueError, "没有藏点"):
            start_game("走廊")

    def test_move_must_follow_map(self):
        result = start_game("主卧", "床底")
        with self.assertRaisesRegex(ValueError, "不相邻"):
            take_turn(result["state_token"], "移动", destination_room="厨房")

    def test_valid_move_preserves_match(self):
        result = start_game("主卧", "床底")
        current = result["player"]["room"]
        destination = ADJ[current][0]
        spot = ROOM_SPOTS[destination][0] if ROOM_SPOTS[destination] else None
        moved = take_turn(
            result["state_token"],
            "移动",
            destination_room=destination,
            hiding_spot=spot,
        )
        self.assertEqual(moved["player"]["room"], destination)
        self.assertIn(moved["status"], {"running", "caught"})

    def test_breath_rebounds_after_limit(self):
        result = start_game("庭院", "竹林后")
        for _ in range(BREATH_MAX):
            result = take_turn(result["state_token"], "屏息")
            if result["status"] == "caught":
                self.fail("远处开局不应在屏息上限前被抓到")
        result = take_turn(result["state_token"], "屏息")
        self.assertIn("憋不住", result["player_action"]["summary"])

    def test_invalid_token_is_friendly_error(self):
        with self.assertRaisesRegex(ValueError, "重新开始"):
            take_turn("not-a-token", "原地不动")


if __name__ == "__main__":
    unittest.main()

