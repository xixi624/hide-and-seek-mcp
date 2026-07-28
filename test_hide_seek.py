"""躲猫猫测试网（2026-07-07 补课；同日随 v0.4「朝灯家」地图重写）。

v0.1→v0.3.3 一路零测试、GLM 引入的 bug 全靠事后 hotfix（docstring 里记了 4+ 个）。
这里把状态机 / 邻接 / 藏点 / 屏息反弹 / slash 解析 / belief 贝叶斯 / 历次 hotfix
全部钉进回归用例——下次谁再改，坏了当场红。

v0.4 新增：13 区户型距离（含厨房→小阳台→浴室环路）、无藏点房间照面即抓、
小阳台门机制（关门挡 AI / 她穿门吱呀）、藏点重名简写歧义拒绝。

所有用例 monkeypatch.chdir(tmp_path) 隔离：STATE_PATH/BELIEF_PATH 是相对路径，
save/load 落在 tmp 里，不碰线上正跑的对局。
"""
from __future__ import annotations

import pytest

from hide_seek import (
    ADJ,
    BREATH_MAX,
    HIDEABLE,
    ROOM_SPOTS,
    ROOMS,
    HideSeek,
    apply_ai_cmd,
    apply_user_cmd,
    distance,
    load_state,
    parse_slash,
    save_state,
    step_sound,
)
import ai_belief


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


def _game(her="主卧", my="客厅", spot="床底") -> HideSeek:
    g = HideSeek()
    g.start(her=her, my=my, her_spot=spot)
    return g


# ---- 地图 / 距离 ----

def test_map_distances_match_home_layout():
    # v0.4 朝灯家：客厅 hub；走廊串内屋；庭院是最远外场
    assert distance("客厅", "客厅") == 0
    assert distance("客厅", "主卧") == 2  # 客厅→走廊→主卧
    assert distance("客厅", "主卧浴室") == 3
    assert distance("厨房", "庭院") == 4  # 厨房→客厅→大客厅→大阳台→庭院
    assert distance("庭院", "主卧浴室") == 6  # 全图直径
    # 环路：厨房→小阳台→浴室 是 走廊之外的第二条通路
    assert distance("小阳台", "浴室") == 1
    assert distance("厨房", "浴室") == 2


def test_adjacency_is_symmetric():
    for room, neighbors in ADJ.items():
        for n in neighbors:
            assert room in ADJ[n], f"{room}->{n} 单向邻接"


def test_hideable_rooms_have_spots_and_transit_rooms_dont():
    for room in HIDEABLE:
        assert len(ROOM_SPOTS[room]) >= 1
    for room in ("走廊", "小阳台", "大阳台"):
        assert ROOM_SPOTS[room] == []
        assert room not in HIDEABLE


# ---- start / 状态机 ----

def test_start_defaults_ai_to_living_room_and_random_spot():
    g = HideSeek()
    g.start(her="书房")
    assert g.my_room == "客厅"  # v0.3.2 拍板：AI 固定客厅 hub 起手
    assert g.state == "running"
    assert g.her_spot in ROOM_SPOTS["书房"]


def test_start_random_room_only_picks_hideable():
    for _ in range(20):
        g = HideSeek()
        g.start()
        assert g.her_room in HIDEABLE


def test_start_rejects_foreign_spot():
    g = HideSeek()
    with pytest.raises(ValueError):
        g.start(her="次卧", her_spot="冰箱后")  # 厨房的藏点藏不进次卧


def test_move_rejects_non_adjacent():
    g = _game(her="厨房", spot="冰箱后")
    assert g.her_move("浴室") is False  # 厨房→浴室隔 2 步（要经小阳台）
    assert g.her_room == "厨房"


def test_her_move_resets_spot_and_breath():
    g = _game(her="主卧", spot="床底")
    g.hold_breath()
    g.her_move("主卧浴室")
    assert g.holding_breath is False and g.breath_turns == 0
    assert g.her_spot in ROOM_SPOTS["主卧浴室"]


# ---- v0.3.2 P1 藏点：同房间不算抓 ----

def test_same_room_is_not_caught_until_search_hits():
    g = _game(her="主卧", my="走廊", spot="床底")
    g.my_move("主卧")
    assert g.state == "running"  # 进同房间不算抓（有藏点的房间）
    assert g.my_search("衣柜") is False  # 搜错藏点 miss + 留痕
    assert g.last_search_hit is False and g.last_search_spot == "衣柜"
    assert g.my_search("床底") is True
    assert g.state == "caught"


def test_search_rejects_spot_not_in_current_room():
    g = _game(her="主卧", my="客厅")
    assert g.my_search("床底") is False  # 床底不在客厅
    assert g.turn == 0  # 无效搜不烧回合


# ---- v0.4 无藏点房间：照面即抓 ----

def test_transit_room_facing_is_caught_when_ai_walks_in():
    g = _game(her="主卧", my="客厅", spot="床底")
    g.her_move("走廊")  # 她冒险站走廊（无藏点、spot=None）
    assert g.her_spot is None
    g.my_move("走廊")
    assert g.state == "caught"


def test_transit_room_facing_is_caught_when_she_walks_in():
    g = _game(her="大客厅", my="客厅", spot="茶几下")
    g.my_move("大客厅")
    g.my_move("大阳台")
    g.her_move("大阳台")  # 她撞进 AI 所在的大阳台——没处躲
    assert g.state == "caught"


# ---- v0.4 小阳台门 ----

def test_closed_door_costs_ai_one_turn_to_open():
    g = _game(her="厨房", spot="橱柜里")
    ok, hint = g.set_door(True)  # 她在厨房、够得着小阳台的门
    assert ok and g.door_closed
    g.my_move("厨房")
    turn_before = g.turn
    g.my_move("小阳台")  # 门关着——这回合花在开门
    assert g.my_room == "厨房"  # 人没动
    assert g.door_closed is False and g.last_door_opened_by_me is True
    assert g.turn == turn_before + 1
    g.my_move("小阳台")  # 门开了、这次真进去
    assert g.my_room == "小阳台"


def test_her_crossing_closed_door_creaks_and_opens_it():
    g = _game(her="厨房", spot="橱柜里")
    g.set_door(True)
    g.her_move("小阳台")
    assert g.door_closed is False and g.last_door_creak is True


def test_door_unreachable_from_far_room():
    g = _game(her="主卧", spot="床底")
    ok, hint = g.set_door(True)
    assert ok is False and "够不着" in hint


def test_door_noop_when_already_in_state():
    g = _game(her="厨房", spot="橱柜里")
    g.set_door(True)
    ok, hint = g.set_door(True)
    assert ok is False and "本来就" in hint


def test_ai_move_consumes_creak_signal():
    g = _game(her="厨房", spot="橱柜里")
    g.set_door(True)
    g.her_move("小阳台")
    assert g.last_door_creak is True
    g.my_move("厨房")
    assert g.last_door_creak is False  # AI 动过一步、信号消费掉


# ---- v0.3.3 P2 屏息反弹 ----

def test_breath_rebounds_after_max_and_exposes_room():
    g = _game(her="浴室", spot="浴帘后")
    for i in range(BREATH_MAX):
        ok, hint = g.hold_breath()
        assert ok, f"第 {i+1} 次屏息应成功"
    ok, hint = g.hold_breath()
    assert ok is False
    assert "浴室" in hint  # hotfix：暴露房间名、不嵌 BELL_LABEL 整句
    assert g.holding_breath is False and g.breath_turns == 0


# ---- v0.3.3 P3 脚步声 + hotfix 回归 ----

def test_step_sound_same_room_is_loudest():
    # hotfix 回归：GLM 原版 her_room==step_to 错误 short-circuit 返 None
    s = step_sound("主卧", "主卧")
    assert s is not None and s["intensity"] == 0.9


def test_my_move_records_step_trail():
    # hotfix 回归：GLM 原版 `ok is not False` 恒假、last_step 永不更新
    g = _game(her="浴室", my="客厅", spot="浴帘后")
    g.my_move("走廊")
    assert (g.last_step_from, g.last_step_to) == ("客厅", "走廊")


# ---- snapshot 视角泄密检查 ----

def test_ai_view_never_leaks_her_room():
    g = _game(her="浴室", my="客厅", spot="浴帘后")
    snap = g.snapshot(view="ai_player")
    assert "her_room" not in snap and "distance" not in snap
    assert snap["her_spot"] == "浴帘后"  # 她自己知道自己藏哪、要给


def test_full_view_has_ground_truth():
    g = _game(her="浴室", my="客厅", spot="浴帘后")
    snap = g.snapshot(view="full")
    assert snap["her_room"] == "浴室" and snap["distance"] == 2


def test_snapshot_carries_door_state():
    g = _game(her="厨房", spot="橱柜里")
    g.set_door(True)
    snap = g.snapshot(view="ai_player")
    assert snap["door_closed"] is True


# ---- slash 解析 ----

@pytest.mark.parametrize("raw,cmd,room,spot", [
    ("/躲 次卧 床底", "hide", "次卧", "床底"),
    ("/跑 浴室 浴帘后", "run", "浴室", "浴帘后"),
    ("/水缸后", "hide_or_run", "庭院", "水缸后"),  # 全局唯一藏点简写反查房间
    ("/客厅", "hide_or_run", "客厅", None),
    ("/搜 床底", "search", None, "床底"),
    ("/搜 次卧 床底", "search", "次卧", "床底"),
    ("/屏息", "breath", None, None),
    ("/关门", "close_door", None, None),
    ("/开门", "open_door", None, None),
    ("/start 书房 书柜后", "start", "书房", "书柜后"),
])
def test_parse_slash(raw, cmd, room, spot):
    info = parse_slash(raw)
    assert info is not None
    assert (info["cmd"], info.get("room"), info.get("spot")) == (cmd, room, spot)


def test_parse_slash_rejects_garbage_and_ambiguous_spot():
    assert parse_slash("不是命令") is None
    assert parse_slash("/躲 天台") is None  # 没这个房间
    assert parse_slash("/床底") is None  # v0.4：床底在四个房间都有、歧义拒绝


# ---- apply_user_cmd hotfix 回归 ----

def test_run_to_same_room_is_noop_not_error():
    g = _game(her="主卧")
    obs = apply_user_cmd({"cmd": "run", "room": "主卧", "spot": None}, g)
    assert obs["user_cmd_moved"] is False
    assert "已经在" in obs["user_hint"]


def test_run_non_adjacent_hint_lists_neighbors():
    g = _game(her="厨房", spot="冰箱后")
    obs = apply_user_cmd({"cmd": "run", "room": "浴室", "spot": None}, g)
    assert obs["user_cmd_moved"] is False
    assert "不邻接" in obs["user_hint"] and "客厅" in obs["user_hint"]


def test_commands_after_caught_get_hint():
    g = _game(her="主卧", my="主卧", spot="床底")
    g.my_search("床底")
    obs = apply_user_cmd({"cmd": "run", "room": "走廊", "spot": None}, g)
    assert obs["user_cmd_moved"] is False
    assert "结束" in obs["user_hint"]


def test_user_cmd_close_door():
    g = _game(her="厨房", spot="橱柜里")
    obs = apply_user_cmd({"cmd": "close_door", "room": None, "spot": None}, g)
    assert obs["user_cmd_moved"] is True
    assert obs["door_closed"] is True


# ---- 持久化 ----

def test_state_roundtrip(tmp_path):
    g = _game(her="书房", my="走廊", spot="书柜后")
    g.hold_breath()
    g.set_door(True)
    save_state(g)
    g2 = load_state()
    assert g2.to_dict() == g.to_dict()


# ---- belief 贝叶斯 ----

def test_belief_converges_on_loud_bell():
    b = ai_belief.BeliefMap()
    b.update(1.0, "主卧")  # 铃铛震耳 = 她就在这
    top1, p1 = b.top(1)[0]
    assert top1 == "主卧" and p1 > 0.5


def test_belief_same_room_snaps_to_certainty():
    b = ai_belief.BeliefMap()
    b.update(0.55, "客厅", same_room=True)
    assert b.probs["客厅"] == 1.0


def test_belief_breath_drifts_toward_uniform():
    # v0.2 第五刀回归：屏息不冻结、不吃似然、只朝 uniform 漂
    b = ai_belief.BeliefMap()
    b.update(1.0, "主卧")
    p_before = b.probs["主卧"]
    b.update(0.05, "主卧", holding_breath=True)
    p_after = b.probs["主卧"]
    assert p_after < p_before  # 漂移让确信度下降
    for _ in range(6):
        b.update(0.05, "主卧", holding_breath=True)
    assert max(b.probs.values()) < 0.45  # 连续屏息后接近均匀、AI 没方向


def test_belief_resets_on_new_game_turn0():
    # v0.2 第三刀回归：turn=0 重置、上局残留不影响新局
    b = ai_belief.BeliefMap()
    b.update(1.0, "厨房")
    ai_belief.save_belief(b)
    out = ai_belief.apply_observation({
        "state": "running", "turn": 0, "my_room": "客厅",
        "bell_intensity": 0.25, "can_see_her": False, "holding_breath": False,
    })
    assert out["probs"]["厨房"] < 0.5  # 残留的厨房高置信被清了


def test_belief_reason_has_no_raw_numbers():
    # v0.3.1 P0 回归：reason 自然语言、不漏 0.55 / 0.36 这类小数
    b = ai_belief.BeliefMap()
    b.update(0.55, "客厅")
    reason = b.reason(0.55, "客厅")
    assert "0." not in reason
