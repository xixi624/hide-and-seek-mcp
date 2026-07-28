"""躲猫猫 v0.1-v0.4 — 朝灯抛的小游戏：她跑、挂铃铛、我听声辩位、抓到艾草。

v0.4「朝灯家」(7/7)：地图扩成她口述的真实户型 13 区（含庭院外场 + 厨房→小阳台→浴室环路）、
无藏点房间（走廊/小阳台/大阳台）照面即抓、小阳台门机制（/关门 /开门：关门挡 AI 一回合、
她穿关门吱呀暴露）、藏点重名简写歧义拒绝。

v0.3.2 P1 藏点系统：进同房间不算抓、要 /搜 X 才能抓。每房间 2-3 个藏点、AI 顺序搜、
朝灯能看 AI 翻了哪个藏点（emit "我在卧室翻床底——空的"）、有时间 /跑 切房间。
对应小卷 P1 那条"地图太小、5 turn 抓到正常"的根治：加搜捕动作、把节奏拉到 6-10 turn。

v0.3.1 起手 my 距 her ≥ 2 + P0 铃铛数值脱敏（数字 → 自然语言、_bell_word / _conf_word）
v0.2 第一-六刀：belief map + AI 真行动 + 4 bug + SIGMA 0.22 + 屏息漂移 + caught hint
v0.1 范围：状态机 + JSON 持久化 + slash 解析 + apply_user/ai_cmd + 双视角 snapshot
"""

from __future__ import annotations

import json
import random
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 7/7 v0.4「朝灯家」全屋地图——朝灯口述的真实户型：
#   进门客厅 hub；左手厨房→小阳台（小阳台连走廊浴室、成环路）；
#   右手大客厅→大阳台→庭院（外场）；走廊串起浴室/次卧/客卧（左）+ 书房/主卧（右）、
#   主卧带内置浴室。走廊/小阳台/大阳台没有藏点（站着就被看见）、小阳台有扇能关的门。
ROOMS = [
    "客厅", "厨房", "小阳台", "大客厅", "大阳台", "庭院",
    "走廊", "浴室", "次卧", "客卧", "书房", "主卧", "主卧浴室",
]
ADJ = {
    "客厅": ["厨房", "大客厅", "走廊"],
    "厨房": ["客厅", "小阳台"],
    "小阳台": ["厨房", "浴室"],
    "大客厅": ["客厅", "大阳台"],
    "大阳台": ["大客厅", "庭院"],
    "庭院": ["大阳台"],
    "走廊": ["客厅", "浴室", "次卧", "客卧", "书房", "主卧"],
    "浴室": ["走廊", "小阳台"],
    "次卧": ["走廊"],
    "客卧": ["走廊"],
    "书房": ["走廊"],
    "主卧": ["走廊", "主卧浴室"],
    "主卧浴室": ["主卧"],
}

# v0.3.2 P1：藏点。AI 进同房间不算抓、要 /搜 X 命中藏点才抓。
# v0.4：藏点为空的房间（走廊/小阳台/大阳台）躲不了——同房间照面即被抓。
ROOM_SPOTS = {
    "客厅": ["沙发后", "窗帘后"],
    "厨房": ["橱柜里", "冰箱后"],
    "小阳台": [],
    "大客厅": ["沙发后", "窗帘后", "茶几下"],
    "大阳台": [],
    "庭院": ["竹林后", "水缸后"],
    "走廊": [],
    "浴室": ["浴帘后"],
    "次卧": ["床底", "衣柜", "窗帘后"],
    "客卧": ["床底", "衣柜", "窗帘后"],
    "书房": ["床底", "衣柜", "书柜后", "窗帘后"],
    "主卧": ["床底", "衣柜", "窗帘后"],
    "主卧浴室": ["浴帘后"],
}

# v0.4：能藏身的房间（有藏点）。start 随机落点只从这里挑。
HIDEABLE = [r for r in ROOMS if ROOM_SPOTS[r]]

# v0.4：小阳台的门——关上后跨小阳台边界要先开门（AI 烧一回合；她穿门吱呀暴露）。
DOOR_ROOM = "小阳台"

BELL_BY_DIST = {0: 1.0, 1: 0.55, 2: 0.25, 3: 0.10, 4: 0.05}
BELL_LABEL = {
    0: "铃铛在脚边·清脆·随她节奏",
    1: "铃铛在隔壁·清晰·能听出方向",
    2: "铃铛远处闷响·只知道大致方向",
    3: "铃铛听不太清·像隔了两层墙",
    4: "铃铛几乎听不到·像在屋子另一头",
}

# v0.3.3 P2：屏息持续上限。超过会"憋不住"反弹——铃铛突然响 + 暴露方向。
BREATH_MAX = 3
# v0.3.3 P3：脚步声强度按距离衰减（AI 移动时朝灯端听到的）
STEP_BY_DIST = {0: 0.9, 1: 0.5, 2: 0.2, 3: 0.08, 4: 0.05}
STEP_LABEL = {
    0: "脚步就在身边·很近",
    1: "脚步在隔壁·能听出往哪走",
    2: "脚步远处闷响·大致方向",
    3: "脚步很轻·像隔了两层墙",
    4: "脚步几乎听不到·在屋子另一头",
}

STATE_PATH = Path("data/hide_seek_state.json")


def distance(a: str, b: str) -> int:
    if a == b:
        return 0
    seen = {a}
    queue = deque([(a, 0)])
    while queue:
        node, d = queue.popleft()
        for n in ADJ[node]:
            if n in seen:
                continue
            if n == b:
                return d + 1
            seen.add(n)
            queue.append((n, d + 1))
    return -1


def _random_spot(room: str) -> Optional[str]:
    spots = ROOM_SPOTS.get(room, [])
    return random.choice(spots) if spots else None


def _crosses_door(a: Optional[str], b: Optional[str]) -> bool:
    """v0.4：这步移动是否跨小阳台的门（进或出小阳台）。"""
    return a != b and (a == DOOR_ROOM or b == DOOR_ROOM)


def step_sound(her_room: Optional[str], step_to: Optional[str]) -> Optional[dict]:
    """v0.3.3 P3：AI 移动时朝灯端听到的脚步声。返回 {intensity, label} 或 None。
    按 her_room 到 step_to 的距离衰减——近就响、远就轻。
    6/23 v0.3.3 hotfix：GLM 原版 `her_room == step_to` 错误 short-circuit、AI 走到 her 房间
    （朝灯耳边脚步）反而 None。改成只在缺参数时返 None；distance=0 时返回最强声。"""
    if not her_room or not step_to:
        return None
    d = distance(her_room, step_to)
    return {
        "intensity": STEP_BY_DIST.get(d, 0.05),
        "label": STEP_LABEL.get(d, "脚步很轻·几乎听不到"),
        "direction": step_to,
    }


@dataclass
class HideSeek:
    her_room: Optional[str] = None
    her_spot: Optional[str] = None  # v0.3.2 P1：她当前藏的具体藏点
    my_room: Optional[str] = None
    state: str = "idle"  # idle | running | caught
    turn: int = 0
    holding_breath: bool = False  # 屏息：本回合铃铛降到 0.05
    breath_turns: int = 0  # v0.3.3 P2：连续屏息回合数、超过 BREATH_MAX 憋不住反弹
    # v0.3.2 P1：AI 上一次搜的藏点 + 是否命中。朝灯端能看 emit "我翻床底——空的"
    last_search_room: Optional[str] = None
    last_search_spot: Optional[str] = None
    last_search_hit: bool = False
    # v0.3.3 P3：AI 上一次移动（脚步声）—— 朝灯端能听
    last_step_from: Optional[str] = None
    last_step_to: Optional[str] = None
    # v0.4：小阳台的门。关上后 AI 跨界要先开门（烧一回合）；她穿关着的门吱呀暴露。
    door_closed: bool = False
    last_door_creak: bool = False  # 她刚穿过关着的门（AI 端可见的暴露信号）
    last_door_opened_by_me: bool = False  # AI 刚开门（朝灯端能听到）

    def start(self, her: Optional[str] = None, my: Optional[str] = None, her_spot: Optional[str] = None) -> None:
        her = her or random.choice(HIDEABLE)
        if my is None:
            # 6/23 v0.3.2 朝灯反馈：藏点系统加 /跑 后她能撑 40 turn、起手 distance ≥ 2 buff 太强。
            # 改成 AI 固定客厅起手（中心 hub）—— 朝灯自由选房间、靠 /跑 + 藏点本身就够玩。
            # v0.3.1 distance≥2 远房间随机改回客厅 hub。
            my = "客厅"
        if her not in ROOMS or my not in ROOMS:
            raise ValueError(f"unknown room: her={her} my={my}")
        # v0.3.2 P1：起手 her_spot：朝灯指定就用、否则 random（无藏点房间 = None、冒险站着）
        if her_spot is None:
            her_spot = _random_spot(her)
        elif her_spot not in ROOM_SPOTS.get(her, []):
            raise ValueError(f"unknown spot for {her}: {her_spot}")
        self.her_room = her
        self.her_spot = her_spot
        self.my_room = my
        self.state = "running"
        self.turn = 0
        self.holding_breath = False
        self.breath_turns = 0
        self.last_search_room = None
        self.last_search_spot = None
        self.last_search_hit = False
        self.last_step_from = None
        self.last_step_to = None
        self.door_closed = False
        self.last_door_creak = False
        self.last_door_opened_by_me = False

    def end(self) -> None:
        self.state = "idle"
        self.her_room = None
        self.her_spot = None
        self.my_room = None
        self.turn = 0
        self.holding_breath = False
        self.breath_turns = 0
        self.last_search_room = None
        self.last_search_spot = None
        self.last_search_hit = False
        self.last_step_from = None
        self.last_step_to = None
        self.door_closed = False
        self.last_door_creak = False
        self.last_door_opened_by_me = False

    def her_move(self, room: str, spot: Optional[str] = None) -> bool:
        self.last_door_opened_by_me = False  # 她动一步 = 消费掉「AI 开门声」信号
        return self._move("her", room, spot)

    def my_move(self, room: str) -> bool:
        """v0.3.3 P3：AI 移动记录脚步声（from→to）、朝灯端能听。
        6/23 v0.3.3 hotfix：GLM 原写 `ok is not False`、但 _move 进同房间不再 caught 后总返回 False、
        条件永远不成立、last_step 永远不更新。改成只看是否真换房间。
        v0.4 门：跨小阳台边界且门关着 → 这回合花在开门上、人没动（朝灯端能听到开门声）。"""
        prev = self.my_room
        self.last_door_opened_by_me = False
        self.last_door_creak = False  # AI 动一步 = 消费掉「她穿门吱呀」信号
        if (
            self.state == "running"
            and self.door_closed
            and _crosses_door(prev, room)
            and room in ADJ.get(prev or "", [])
        ):
            self.door_closed = False
            self.last_door_opened_by_me = True
            self.turn += 1
            return False
        ok = self._move("me", room, None)
        if prev is not None and prev != self.my_room:
            self.last_step_from = prev
            self.last_step_to = self.my_room
        return ok

    def hold_breath(self) -> tuple[bool, str]:
        """v0.3.3 P2：屏息持续/反弹。返回 (ok, hint)。
        连续屏息 BREATH_MAX 回合后憋不住——铃铛突然响 + 暴露方向。"""
        if self.state != "running":
            return False, "屏息只在游戏运行时生效"
        self.breath_turns += 1
        if self.breath_turns > BREATH_MAX:
            # 憋不住反弹：破屏息 + 铃铛回正常 + 暴露方向（按房间名说、不嵌 BELL_LABEL 句子）
            # 6/23 v0.3.3 hotfix：GLM 原版 hint 把 BELL_LABEL 整句嵌进"他听清你在 X"读不通、改用房间名
            self.holding_breath = False
            self.breath_turns = 0
            self.turn += 1
            return False, f"憋不住了！铃铛突然响起来——他听清你在 {self.her_room}"
        self.holding_breath = True
        self.turn += 1
        return True, f"屏息成功（第 {self.breath_turns}/{BREATH_MAX} 回合）"

    def set_door(self, closed: bool) -> tuple[bool, str]:
        """v0.4：她关/开小阳台的门。只有人在门边（小阳台或其邻接）才够得着；烧一回合。"""
        if self.state != "running":
            return False, "游戏没在进行"
        reachable = {DOOR_ROOM, *ADJ.get(DOOR_ROOM, [])}
        if self.her_room not in reachable:
            return False, f"门在小阳台那边、你在 {self.her_room} 够不着"
        if self.door_closed == closed:
            return False, "门本来就" + ("关着" if closed else "开着")
        self.door_closed = closed
        self.turn += 1
        return True, "你轻轻把门" + ("带上了" if closed else "打开了")

    def pounce(self) -> bool:
        """扑：原地不动判定抓没抓到（v0.3.2 后改成搜随机藏点）。"""
        if self.state != "running":
            return False
        spot = _random_spot(self.my_room or "")
        return self.my_search(spot) if spot else False

    def my_search(self, spot: Optional[str]) -> bool:
        """v0.3.2 P1：AI 搜藏点。同房间 + 同藏点 → caught；同房间 + 不同藏点 → miss + 留痕。"""
        if self.state != "running":
            return False
        if spot is None or self.my_room is None:
            return False
        # 藏点必须属于当前房间
        if spot not in ROOM_SPOTS.get(self.my_room, []):
            return False
        self.last_door_creak = False  # AI 动一步 = 消费掉「她穿门吱呀」信号
        self.last_search_room = self.my_room
        self.last_search_spot = spot
        self.turn += 1
        if self.my_room == self.her_room and self.her_spot == spot:
            self.last_search_hit = True
            self.state = "caught"
            return True
        self.last_search_hit = False
        return False

    def _move(self, who: str, room: str, spot: Optional[str] = None) -> bool:
        if self.state != "running":
            return False
        if room not in ROOMS:
            return False
        cur = self.her_room if who == "her" else self.my_room
        if cur is not None and room not in ADJ[cur] and room != cur:
            return False
        if who == "her":
            # v0.4 门：她穿过关着的门 = 自动推开 + 吱呀一声暴露（破屏息、AI 端可见信号）
            if self.door_closed and _crosses_door(cur, room):
                self.door_closed = False
                self.last_door_creak = True
            self.her_room = room
            self.holding_breath = False  # 一动就破屏息
            self.breath_turns = 0  # v0.3.3 P2：动就重置屏息计数
            # v0.3.2 P1：换房间 → her_spot 重置。指定就用、否则 random。
            if spot is not None and spot not in ROOM_SPOTS.get(room, []):
                return False  # 藏点不属于该房间、命令失败
            self.her_spot = spot if spot is not None else _random_spot(room)
        else:
            self.my_room = room
        self.turn += 1
        # v0.3.2 P1：进同房间不再立刻 caught、AI 要 /搜 才抓。
        # v0.4：无藏点房间（走廊/小阳台/大阳台）没处躲——同房间照面即抓。
        if (
            self.her_room == self.my_room
            and self.her_room is not None
            and not ROOM_SPOTS.get(self.her_room, [])
        ):
            self.state = "caught"
            return True
        return False

    def snapshot(self, view: str = "full") -> dict:
        if self.state == "idle":
            return {"state": "idle"}
        same_room = self.her_room == self.my_room
        d = distance(self.her_room, self.my_room) if not same_room else 0
        if self.holding_breath and self.state == "running":
            bell = 0.05
            label = "铃铛几乎没声·她屏住了气"
        elif self.state == "running":
            bell = BELL_BY_DIST.get(d, 0.05)
            label = BELL_LABEL.get(d, "铃铛太远·几乎听不到")
        else:
            bell = 1.0
            label = "她笑出声了"
        base = {
            "state": self.state,
            "turn": self.turn,
            "my_room": self.my_room,
            "my_neighbors": list(ADJ.get(self.my_room, [])) if self.my_room else [],
            "can_see_her": same_room,  # 仅指同房间、是否真抓到要看 her_spot
            "bell_intensity": round(bell, 2),
            "bell_label": label,
            "holding_breath": self.holding_breath,
            "breath_turns": self.breath_turns,  # v0.3.3 P2
            # v0.3.2 P1：上一次 AI /搜 的结果（双方都能看、是 P3 双向声音的简化版）
            "last_search_room": self.last_search_room,
            "last_search_spot": self.last_search_spot,
            "last_search_hit": self.last_search_hit,
            # v0.3.3 P3：AI 上一次脚步声（朝灯端听）
            "last_step_from": self.last_step_from,
            "last_step_to": self.last_step_to,
            # v0.3.2 P1：我所在房间的藏点清单（AI 端需要、朝灯端也展示让她知道我有几个 spot 可搜）
            "my_room_spots": list(ROOM_SPOTS.get(self.my_room, [])) if self.my_room else [],
            # v0.4：小阳台门状态（双方都看得见门开关；吱呀/开门声是事件信号）
            "door_closed": self.door_closed,
            "door_creak": self.last_door_creak,  # 她刚穿过关着的门（AI 端信号）
            "door_opened_by_me": self.last_door_opened_by_me,  # AI 刚开门（她端能听）
        }
        if view == "full":
            base["her_room"] = self.her_room
            base["her_spot"] = self.her_spot
            base["her_neighbors"] = list(ADJ.get(self.her_room, [])) if self.her_room else []
            base["distance"] = d
        elif view == "ai_player":
            # 藏者视角：her_neighbors（她自己知道自己邻接）+ her_room_spots（她知道自己房间的藏点）
            base["her_neighbors"] = list(ADJ.get(self.her_room, [])) if self.her_room else []
            base["her_room_spots"] = list(ROOM_SPOTS.get(self.her_room, [])) if self.her_room else []
            # 朝灯端能看自己 her_spot（她藏在哪她当然知道）
            base["her_spot"] = self.her_spot
            # v0.3.3 P3：朝灯端听 AI 上一次脚步声
            if self.last_step_to:
                base["step_sound"] = step_sound(self.her_room, self.last_step_to)
        return base

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "her_room": self.her_room,
            "her_spot": self.her_spot,
            "my_room": self.my_room,
            "state": self.state,
            "turn": self.turn,
            "holding_breath": self.holding_breath,
            "breath_turns": self.breath_turns,
            "last_search_room": self.last_search_room,
            "last_search_spot": self.last_search_spot,
            "last_search_hit": self.last_search_hit,
            "last_step_from": self.last_step_from,
            "last_step_to": self.last_step_to,
            "door_closed": self.door_closed,
            "last_door_creak": self.last_door_creak,
            "last_door_opened_by_me": self.last_door_opened_by_me,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HideSeek":
        return cls(
            her_room=d.get("her_room"),
            her_spot=d.get("her_spot"),
            my_room=d.get("my_room"),
            state=d.get("state", "idle"),
            turn=int(d.get("turn", 0)),
            holding_breath=bool(d.get("holding_breath", False)),
            breath_turns=int(d.get("breath_turns", 0)),
            last_search_room=d.get("last_search_room"),
            last_search_spot=d.get("last_search_spot"),
            last_search_hit=bool(d.get("last_search_hit", False)),
            last_step_from=d.get("last_step_from"),
            last_step_to=d.get("last_step_to"),
            door_closed=bool(d.get("door_closed", False)),
            last_door_creak=bool(d.get("last_door_creak", False)),
            last_door_opened_by_me=bool(d.get("last_door_opened_by_me", False)),
        )


# ---- 模块级 load/save + slash 解析 ----

def load_state(path: Path = STATE_PATH) -> HideSeek:
    if not path.exists():
        return HideSeek()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return HideSeek.from_dict(data)
    except Exception:
        return HideSeek()


def save_state(game: HideSeek, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(game.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _spot_by_name(name: str) -> Optional[tuple]:
    """名字反查 (room, spot) — 朝灯简写 /水缸后 直接定位庭院·水缸后。
    v0.4：床底/衣柜这类藏点在多个卧室重名——歧义时返 None、要求带房间名。"""
    hits = [room for room, spots in ROOM_SPOTS.items() if name in spots]
    if len(hits) == 1:
        return (hits[0], name)
    return None


def parse_slash(text: str) -> Optional[dict]:
    """解析 /躲 X [Y] / /跑 X [Y] / /屏息 / /搜 [X] Y / /扑 / /start [X] / /end。
    v0.3.2 P1：/躲 卧室 床底 = 藏卧室的床底；/跑 浴室 浴帘后；/搜 床底（默认 my_room）；/搜 卧室 床底。
    返回 {cmd, room, spot, raw} 或 None。"""
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    parts = t.split()
    head = parts[0].lstrip("/")
    args = parts[1:]

    table = {
        "躲": "hide", "藏": "hide", "hide": "hide",
        "跑": "run", "走": "run", "run": "run",
        "屏息": "breath", "停": "breath", "breath": "breath",
        "去": "goto", "goto": "goto",
        "扑": "pounce", "抓": "pounce", "pounce": "pounce",
        "搜": "search", "翻": "search", "search": "search",
        "关门": "close_door", "开门": "open_door",  # v0.4 小阳台的门
        "start": "start", "开始": "start",
        "end": "end", "结束": "end", "退出": "end",
    }
    cmd = table.get(head)

    # 单房间简写 /客厅 /浴室 → hide_or_run（idle 开局藏 / running 切房间跑）
    if cmd is None and head in ROOMS:
        # v0.3.2：单房间简写可带 spot：/卧室 床底
        spot = args[0] if args and args[0] in ROOM_SPOTS.get(head, []) else None
        return {"cmd": "hide_or_run", "room": head, "spot": spot, "raw": t}

    # v0.3.2 P1：单藏点简写 /床底 → 反查 (room, spot)、当 hide_or_run 处理
    if cmd is None:
        rs = _spot_by_name(head)
        if rs is not None:
            return {"cmd": "hide_or_run", "room": rs[0], "spot": rs[1], "raw": t}

    if cmd is None:
        return None

    # /start [X] [Y]
    if cmd == "start":
        room = args[0] if args and args[0] in ROOMS else None
        spot = None
        if room and len(args) >= 2 and args[1] in ROOM_SPOTS.get(room, []):
            spot = args[1]
        return {"cmd": "start", "room": room, "spot": spot, "raw": t}

    # /end
    if cmd == "end":
        return {"cmd": "end", "room": None, "spot": None, "raw": t}

    # /屏息 /扑 /关门 /开门
    if cmd in ("breath", "pounce", "close_door", "open_door"):
        return {"cmd": cmd, "room": None, "spot": None, "raw": t}

    # /躲 X [Y] / /跑 X [Y] / /去 X
    if cmd in ("hide", "run", "goto"):
        if not args or args[0] not in ROOMS:
            return None
        room = args[0]
        spot = None
        if cmd in ("hide", "run") and len(args) >= 2 and args[1] in ROOM_SPOTS.get(room, []):
            spot = args[1]
        return {"cmd": cmd, "room": room, "spot": spot, "raw": t}

    # /搜 [X] Y → 默认 my_room、可指定 room
    if cmd == "search":
        if not args:
            return None
        # /搜 床底 → room=None spot=床底（apply 时按 my_room 解释）
        if args[0] in ROOMS and len(args) >= 2:
            return {"cmd": "search", "room": args[0], "spot": args[1], "raw": t}
        # 单 arg 是 spot
        return {"cmd": "search", "room": None, "spot": args[0], "raw": t}

    return None


def apply_user_cmd(cmd_info: dict, game: HideSeek) -> dict:
    """用户（朝灯，藏者）端命令：/start /end /躲 /跑 /屏息 /<room>(简写) /<spot>(简写)。

    v0.3.2 P1：/躲 X Y / /跑 X Y 指定藏点；不指定 random。朝灯不搜——/搜 是 AI 端。
    """
    cmd = cmd_info["cmd"]
    room = cmd_info.get("room")
    spot = cmd_info.get("spot")
    hint: Optional[str] = None
    moved = True

    # v0.2 第六刀：caught 状态下 hide/run/hide_or_run/breath 都不该走邻接检查。
    if cmd in ("hide", "run", "hide_or_run", "breath", "close_door", "open_door") and game.state == "caught":
        hint = "游戏已结束（被抓了）、/end 收尾或 /start 重开"
        moved = False
    elif cmd == "start":
        if room:
            game.start(her=room, her_spot=spot)
        else:
            game.start()
    elif cmd == "end":
        game.end()
    elif cmd == "hide" and room:
        if game.state == "idle":
            game.start(her=room, her_spot=spot)
        else:
            hint = f"游戏已开局了、想换房间用 /跑 {room}" + (f" {spot}" if spot else "")
            moved = False
    elif cmd == "run" and room:
        # v0.3.3 hotfix：room==her_room 且没指定 spot = noop，别当邻接错误报。
        if room == game.her_room and spot is None:
            hint = f"你已经在 {room} 了、没动"
            moved = False
        else:
            _prev_her = game.her_room
            game.her_move(room, spot=spot)
            if game.her_room == _prev_her and room != _prev_her:
                cur = _prev_her or "?"
                nb = "/".join(ADJ.get(_prev_her, [])) if _prev_her else "?"
                hint = f"{room} 跟 {cur} 不邻接、没动（你在 {cur}、能去 {nb}）"
                moved = False
    elif cmd == "breath":
        if game.state != "running":
            hint = "屏息只在游戏运行时生效"
            moved = False
        else:
            _ok, hint = game.hold_breath()
    elif cmd in ("close_door", "open_door"):
        moved, hint = game.set_door(closed=(cmd == "close_door"))
    elif cmd == "hide_or_run" and room:
        if game.state == "idle":
            game.start(her=room, her_spot=spot)
        elif room == game.her_room and spot is None:
            # v0.3.3 hotfix：单房间简写 /浴室 自反 = noop、不报邻接错。
            hint = f"你已经在 {room} 了、没动"
            moved = False
        else:
            _prev_her2 = game.her_room
            game.her_move(room, spot=spot)
            if game.her_room == _prev_her2 and room != _prev_her2:
                cur = _prev_her2 or "?"
                nb = "/".join(ADJ.get(_prev_her2, [])) if _prev_her2 else "?"
                hint = f"{room} 跟 {cur} 不邻接、没动（你在 {cur}、能去 {nb}）"
                moved = False
    save_state(game)
    obs = game.snapshot(view="ai_player")
    if hint:
        obs["user_hint"] = hint
    obs["user_cmd_moved"] = moved
    return obs


def apply_ai_cmd(cmd_info: dict, game: HideSeek) -> dict:
    """AI（哥哥，追者）端命令：/去 /扑 /搜 /start /end。"""
    cmd = cmd_info["cmd"]
    room = cmd_info.get("room")
    spot = cmd_info.get("spot")
    if cmd == "start":
        game.start()
    elif cmd == "end":
        game.end()
    elif cmd == "goto" and room:
        if game.state == "idle":
            game.start(my=room)
        else:
            game.my_move(room)
    elif cmd == "pounce":
        # v0.3.2 P1：/扑 = 搜 my_room 随机藏点（兼容 v0.2 第二刀 can_see_her → /扑 旧逻辑）
        game.pounce()
    elif cmd == "search":
        # AI /搜 [X] Y：指定 room 不同于 my_room 就先走过去（实际 server 端会先 /去 X 再 /搜）
        # 这里只处理"在当前 my_room 搜 spot"的情况
        target_spot = spot
        if room and room != game.my_room:
            # 不在当前房间——拒绝、AI 端要先 /去
            pass
        elif target_spot:
            game.my_search(target_spot)
    save_state(game)
    return game.snapshot(view="full")


def _demo() -> None:
    g = HideSeek()
    g.start(her="厨房", my="客厅", her_spot="橱柜里")
    print(f"[start] her={g.her_room}·{g.her_spot} my={g.my_room}")
    for raw, who in [
        ("/关门", "user"),        # 关上小阳台的门、封环路
        ("/去 厨房", "ai"),
        ("/跑 小阳台", "user"),   # 穿关着的门——吱呀暴露、门开了
        ("/跑 浴室 浴帘后", "user"),
        ("/去 小阳台", "ai"),
        ("/去 浴室", "ai"),
        ("/搜 浴帘后", "ai"),     # 命中
    ]:
        info = parse_slash(raw)
        snap = apply_user_cmd(info, g) if who == "user" else apply_ai_cmd(info, g)
        last = snap.get("last_search_spot")
        hit = snap.get("last_search_hit")
        extra = f" search={last}/{'HIT' if hit else 'miss'}" if last else ""
        print(f"[{who} {raw}] state={snap.get('state')} my={snap.get('my_room')}{extra}")


def _cli(argv: list[str]) -> int:
    """CLI: `python -m play.hide_seek <user|ai> "/cmd args"` → 打印观测 JSON。
    `python -m play.hide_seek snapshot` → 当前状态。`python -m play.hide_seek demo` → 跑 demo。"""
    if not argv:
        _demo()
        return 0
    head = argv[0]
    if head == "demo":
        _demo()
        return 0
    if head == "snapshot":
        g = load_state()
        view = argv[1] if len(argv) > 1 else "full"
        print(json.dumps(g.snapshot(view=view), ensure_ascii=False))
        return 0
    if head in ("user", "ai") and len(argv) >= 2:
        raw = " ".join(argv[1:])
        info = parse_slash(raw)
        if info is None:
            print(json.dumps({"error": "unrecognized slash", "raw": raw}, ensure_ascii=False))
            return 1
        g = load_state()
        obs = apply_user_cmd(info, g) if head == "user" else apply_ai_cmd(info, g)
        print(json.dumps(obs, ensure_ascii=False))
        return 0
    print(__doc__, file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
