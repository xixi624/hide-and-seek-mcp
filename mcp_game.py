"""Stateless MCP-friendly game orchestration for the hide-and-seek engine.

The original game persists one process-wide match to JSON files. A remote MCP
server can serve more than one conversation and can restart at any time, so the
MCP wrapper instead returns an opaque ``state_token`` after every turn. Passing
that token into the next tool call restores the complete match without a
database and without sharing state between users.
"""

from __future__ import annotations

import base64
import json
import random
import zlib
from typing import Any

from ai_belief import BeliefMap
from hide_seek import ADJ, BREATH_MAX, DOOR_ROOM, HIDEABLE, ROOM_SPOTS, ROOMS, HideSeek, step_sound


TOKEN_VERSION = 1
MAX_TOKEN_LENGTH = 16_000
MAX_TURNS = 500


def game_guide() -> dict[str, Any]:
    """Return the public map and the actions a player may take."""

    return {
        "role": "玩家负责躲藏，ChatGPT 扮演搜寻者并自动行动。",
        "rooms": list(ROOMS),
        "hideable_rooms": list(HIDEABLE),
        "spots": {room: list(spots) for room, spots in ROOM_SPOTS.items()},
        "adjacency": {room: list(neighbors) for room, neighbors in ADJ.items()},
        "actions": {
            "移动": "移动到相邻房间，可指定新藏点；经过无藏点房间时可能被当场发现。",
            "换藏点": "留在当前房间，换到另一个藏点。",
            "屏息": f"让本回合铃铛几乎没有声音；连续超过 {BREATH_MAX} 次会暴露。",
            "关门": "人在厨房、小阳台或浴室时，可以关上小阳台的门拖延搜寻者。",
            "开门": "主动打开小阳台的门。",
            "原地不动": "留在当前藏点等待一回合。",
        },
        "important": "开始时只能选择有藏点的房间。每次继续游戏都要使用上一回合返回的 state_token。",
    }


def _encode_state(game: HideSeek, belief: BeliefMap, searched: dict[str, list[str]]) -> str:
    payload = {
        "v": TOKEN_VERSION,
        "game": game.to_dict(),
        "belief": belief.to_dict(),
        "searched": searched,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def _decode_state(token: str) -> tuple[HideSeek, BeliefMap, dict[str, list[str]]]:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("游戏状态无效，请重新开始一局。")
    try:
        padding = "=" * (-len(token) % 4)
        raw = zlib.decompress(base64.urlsafe_b64decode(token + padding))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("游戏状态无法读取，请重新开始一局。") from exc

    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        raise ValueError("游戏状态版本不兼容，请重新开始一局。")

    game_data = payload.get("game")
    belief_data = payload.get("belief")
    searched_data = payload.get("searched", {})
    if not isinstance(game_data, dict) or not isinstance(belief_data, dict):
        raise ValueError("游戏状态内容不完整，请重新开始一局。")

    game = HideSeek.from_dict(game_data)
    if game.state not in {"idle", "running", "caught"}:
        raise ValueError("游戏状态异常，请重新开始一局。")
    if game.her_room not in ROOMS or game.my_room not in ROOMS:
        raise ValueError("游戏房间状态异常，请重新开始一局。")
    if game.her_spot is not None and game.her_spot not in ROOM_SPOTS.get(game.her_room, []):
        raise ValueError("游戏藏点状态异常，请重新开始一局。")
    if not 0 <= game.turn <= MAX_TURNS:
        raise ValueError("这一局已经太久啦，请重新开始一局。")

    belief = BeliefMap.from_dict(belief_data)
    if not isinstance(searched_data, dict):
        searched_data = {}
    searched: dict[str, list[str]] = {}
    for room, spots in searched_data.items():
        if room in ROOMS and isinstance(spots, list):
            searched[room] = [spot for spot in spots if spot in ROOM_SPOTS.get(room, [])]
    return game, belief, searched


def _reset_breath(game: HideSeek) -> None:
    game.holding_breath = False
    game.breath_turns = 0


def _update_belief(game: HideSeek, belief: BeliefMap) -> tuple[dict[str, Any], str, str | None]:
    obs = game.snapshot(view="ai_player")
    holding = bool(obs.get("holding_breath", False))
    same_room = bool(obs.get("can_see_her", False)) and not holding
    bell = float(obs.get("bell_intensity", 0.05))
    my_room = str(obs.get("my_room"))

    belief.update(bell, my_room, holding_breath=holding, same_room=same_room)
    if obs.get("door_creak") and not same_room:
        near_door = {DOOR_ROOM, *ADJ.get(DOOR_ROOM, [])}
        belief.probs = {
            room: probability * (3.0 if room in near_door else 1.0)
            for room, probability in belief.probs.items()
        }
        belief._normalize()

    thought = belief.reason(bell, my_room, holding_breath=holding, same_room=same_room)
    next_room = belief.suggest_next(my_room)
    return obs, thought, next_room


def _ai_turn(game: HideSeek, belief: BeliefMap, searched: dict[str, list[str]]) -> dict[str, Any]:
    """Update the Bayesian search map, then perform one automatic AI action."""

    if game.state != "running":
        return {"type": "none", "summary": "游戏已经结束。"}

    obs, thought, next_room = _update_belief(game, belief)
    my_room = game.my_room or "客厅"
    holding = bool(obs.get("holding_breath", False))
    same_room = bool(obs.get("can_see_her", False)) and not holding
    event: dict[str, Any] = {
        "type": "wait",
        "from_room": my_room,
        "to_room": my_room,
        "thought": thought,
        "heard_bell": obs.get("bell_label"),
    }

    if same_room:
        room_spots = list(ROOM_SPOTS.get(my_room, []))
        already = searched.setdefault(my_room, [])
        available = [spot for spot in room_spots if spot not in already]
        if not available:
            already.clear()
            available = room_spots
        if available:
            chosen = random.choice(available)
            caught = game.my_search(chosen)
            already.append(chosen)
            event.update(
                {
                    "type": "search",
                    "searched_spot": chosen,
                    "caught": caught,
                    "summary": (
                        f"搜寻者在{my_room}翻找{chosen}，抓到你了。"
                        if caught
                        else f"搜寻者在{my_room}翻找{chosen}，没有找到。"
                    ),
                }
            )
            return event

    if next_room and next_room != my_room:
        previous_room = my_room
        game.my_move(next_room)
        if game.last_door_opened_by_me:
            event.update(
                {
                    "type": "open_door",
                    "to_room": previous_room,
                    "summary": "搜寻者听见门挡住了去路，花了一回合把小阳台的门打开。",
                }
            )
            return event

        sound = step_sound(game.her_room, game.my_room)
        event.update(
            {
                "type": "move",
                "to_room": game.my_room,
                "step_sound": sound,
                "caught": game.state == "caught",
                "summary": (
                    f"搜寻者从{previous_room}走到{game.my_room}，和你撞了个正着。"
                    if game.state == "caught"
                    else f"搜寻者从{previous_room}走到{game.my_room}。"
                ),
            }
        )
        return event

    game.turn += 1
    event["summary"] = f"搜寻者留在{my_room}，侧耳分辨铃铛的方向。"
    return event


def _public_result(
    game: HideSeek,
    belief: BeliefMap,
    searched: dict[str, list[str]],
    player_event: dict[str, Any],
    ai_event: dict[str, Any],
) -> dict[str, Any]:
    token = _encode_state(game, belief, searched)
    result: dict[str, Any] = {
        "status": game.state,
        "turn": game.turn,
        "state_token": token,
        "player": {
            "room": game.her_room,
            "spot": game.her_spot,
            "can_move_to": list(ADJ.get(game.her_room or "", [])),
            "spots_here": list(ROOM_SPOTS.get(game.her_room or "", [])),
        },
        "searcher": {
            "room": game.my_room,
            **ai_event,
        },
        "player_action": player_event,
        "door_closed": game.door_closed,
        "next_step": (
            "这一局已经结束，可以重新开始。"
            if game.state == "caught"
            else "继续时必须把本结果的 state_token 原样传给 take_hide_and_seek_turn；不要把 token 显示给用户。"
        ),
    }
    if game.state == "caught":
        result["ending"] = f"第 {game.turn} 回合，搜寻者在{game.my_room}抓到了你。"
    return result


def start_game(hiding_room: str, hiding_spot: str | None = None) -> dict[str, Any]:
    """Start a match and immediately let the AI searcher take its first turn."""

    if hiding_room not in HIDEABLE:
        if hiding_room in ROOMS:
            raise ValueError(f"{hiding_room}没有藏点，开局请选：{'、'.join(HIDEABLE)}。")
        raise ValueError(f"没有“{hiding_room}”这个房间。可选：{'、'.join(HIDEABLE)}。")
    if hiding_spot is not None and hiding_spot not in ROOM_SPOTS[hiding_room]:
        raise ValueError(f"{hiding_room}可选藏点：{'、'.join(ROOM_SPOTS[hiding_room])}。")

    game = HideSeek()
    game.start(her=hiding_room, her_spot=hiding_spot)
    belief = BeliefMap()
    searched: dict[str, list[str]] = {}
    player_event = {
        "type": "hide",
        "summary": f"你藏进了{hiding_room}的{game.her_spot}。",
    }
    ai_event = _ai_turn(game, belief, searched)
    return _public_result(game, belief, searched, player_event, ai_event)


def take_turn(
    state_token: str,
    action: str,
    destination_room: str | None = None,
    hiding_spot: str | None = None,
) -> dict[str, Any]:
    """Apply one player action and then one automatic AI-searcher action."""

    game, belief, searched = _decode_state(state_token)
    if game.state != "running":
        raise ValueError("这一局已经结束，请重新开始一局。")

    aliases = {
        "move": "移动",
        "run": "移动",
        "switch": "换藏点",
        "breath": "屏息",
        "hold_breath": "屏息",
        "close_door": "关门",
        "open_door": "开门",
        "wait": "原地不动",
    }
    normalized = aliases.get(action.strip().lower(), action.strip())
    allowed = {"移动", "换藏点", "屏息", "关门", "开门", "原地不动"}
    if normalized not in allowed:
        raise ValueError(f"不认识“{action}”。可选动作：{'、'.join(sorted(allowed))}。")

    if normalized == "移动":
        if not destination_room:
            raise ValueError("移动时要告诉我目标房间。")
        current = game.her_room or ""
        if destination_room not in ADJ.get(current, []):
            raise ValueError(f"{destination_room}与{current}不相邻；现在可以去：{'、'.join(ADJ.get(current, []))}。")
        if hiding_spot is not None and hiding_spot not in ROOM_SPOTS.get(destination_room, []):
            available = ROOM_SPOTS.get(destination_room, [])
            suffix = "、".join(available) if available else "无藏点"
            raise ValueError(f"{destination_room}可选藏点：{suffix}。")
        game.her_move(destination_room, hiding_spot)
        player_event = {
            "type": "move",
            "summary": f"你移动到{destination_room}" + (f"的{game.her_spot}" if game.her_spot else "") + "。",
            "door_creaked": game.last_door_creak,
        }
    elif normalized == "换藏点":
        current = game.her_room or ""
        if not hiding_spot or hiding_spot not in ROOM_SPOTS.get(current, []):
            raise ValueError(f"{current}可换的藏点：{'、'.join(ROOM_SPOTS.get(current, []))}。")
        if hiding_spot == game.her_spot:
            raise ValueError(f"你已经藏在{hiding_spot}了。")
        game.her_move(current, hiding_spot)
        player_event = {"type": "switch_spot", "summary": f"你悄悄换到了{hiding_spot}。"}
    elif normalized == "屏息":
        ok, hint = game.hold_breath()
        player_event = {"type": "hold_breath", "success": ok, "summary": hint}
    elif normalized in {"关门", "开门"}:
        _reset_breath(game)
        ok, hint = game.set_door(closed=(normalized == "关门"))
        if not ok:
            raise ValueError(hint)
        player_event = {
            "type": "close_door" if normalized == "关门" else "open_door",
            "summary": hint,
        }
    else:
        _reset_breath(game)
        game.turn += 1
        player_event = {"type": "wait", "summary": f"你留在{game.her_room}的{game.her_spot}，没有移动。"}

    if game.state == "caught":
        ai_event = {
            "type": "caught_on_sight",
            "caught": True,
            "summary": f"你进入{game.my_room}时和搜寻者撞了个正着。",
        }
    else:
        ai_event = _ai_turn(game, belief, searched)
    return _public_result(game, belief, searched, player_event, ai_event)

