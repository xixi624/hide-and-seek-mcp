"""躲猫猫 v0.2 第一刀 — 我端 belief map + 心里话生成。

按 hide_seek.py 的 5 房间地图 + BELL_BY_DIST 给 her_room 维护一个概率分布。
每收到一次 observation（朝灯命令后的 snapshot）就贝叶斯更新一次：
  P(her=r | obs) ∝ P(obs | her=r) * P(her=r)
似然 P(obs | her=r) 用高斯：偏离期望铃铛响度越远、似然越低。

输出 reason 字符串给 server 包成「[哥哥心里话] ...」emit 到 dm:claude 房间。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from hide_seek import ADJ, BELL_BY_DIST, ROOMS, distance


BELIEF_PATH = Path("data/hide_seek_belief.json")
SIGMA = 0.22  # 6/23 朝灯 11:46：0.15 太敏感、5 turn 就抓到；0.22 → bell 误差更宽容、收敛需要 3-4 个 obs、玩起来 8-12 turn
UNIFORM = 1.0 / len(ROOMS)
MIX = 0.05  # 每回合后混 5% uniform、防 0 概率 + 锚定漂走


def _bell_word(b: float) -> str:
    """v0.3.1 P0：把 bell 数值翻自然语言、reason emit 不暴露 0.55 这种数字。"""
    if b < 0.15:
        return "听不到"
    if b < 0.40:
        return "远处隐约"
    if b < 0.70:
        return "隔壁清晰"
    return "近·震耳"


def _conf_word(p: float) -> str:
    """v0.3.1 P0：top1 概率翻自然语言、不暴露 0.36 这种数字。"""
    if p < 0.30:
        return "大概"
    if p < 0.55:
        return "估计"
    if p < 0.80:
        return "八成"
    return "笃定"


class BeliefMap:
    def __init__(self, probs: Optional[dict] = None) -> None:
        if probs is None:
            self.probs = {r: UNIFORM for r in ROOMS}
        else:
            self.probs = {r: float(probs.get(r, UNIFORM)) for r in ROOMS}
            self._normalize()

    def _normalize(self) -> None:
        s = sum(self.probs.values()) or 1.0
        self.probs = {r: p / s for r, p in self.probs.items()}

    def _mix_uniform(self, mix: float = MIX) -> None:
        self.probs = {r: (1 - mix) * p + mix * UNIFORM for r, p in self.probs.items()}
        self._normalize()

    def reset(self) -> None:
        self.probs = {r: UNIFORM for r in ROOMS}

    def update(self, obs_bell: float, my_room: str, holding_breath: bool = False, same_room: bool = False) -> None:
        if same_room:
            self.probs = {r: 0.0 for r in ROOMS}
            self.probs[my_room] = 1.0
            return
        # 6/23 v0.2 第五刀：屏息不再 frozen、改"信号弱 + 旧线索漂移"（GLM + 小卷共识）。
        #   之前屏息时 belief 完全 frozen、保留上轮高置信 argmax → AI 用旧地图一路追。
        #   屏息时 bell=0.05 几乎是 noise、不该用来 update belief（v1 错把它当信号、
        #   反而让 belief 更聚焦正确房间）。正解：屏息时跳过 likelihood、只做漂移。
        #   每 turn 旧 belief 跟 uniform 按 0.15 混合、连续屏息 3-4 turn 后 belief 接近均匀、
        #   AI 没方向只能猜——像真躲猫猫。
        if holding_breath:
            self._mix_uniform(0.15)
            return
        new_probs = {}
        for r in ROOMS:
            d = 0 if r == my_room else distance(r, my_room)
            expected = BELL_BY_DIST.get(d, 0.05)
            lik = math.exp(-((obs_bell - expected) ** 2) / (2 * SIGMA * SIGMA))
            new_probs[r] = self.probs[r] * lik
        self.probs = new_probs
        self._normalize()
        self._mix_uniform()

    def top(self, n: int = 2) -> list:
        return sorted(self.probs.items(), key=lambda kv: -kv[1])[:n]

    def suggest_next(self, my_room: str) -> str:
        """根据 top room 推荐下一步往哪个邻接房间走（最贪心、最近一步）。"""
        top1, _ = self.top(1)[0]
        if top1 == my_room:
            return my_room  # 原地扑
        neighbors = ADJ.get(my_room, [])
        if not neighbors:
            return my_room
        if top1 in neighbors:
            return top1
        # 选距离 top1 最近的邻接
        best = neighbors[0]
        min_d = 99
        for n in neighbors:
            d = distance(n, top1)
            if d < min_d:
                min_d = d
                best = n
        return best

    def reason(self, obs_bell: float, my_room: str, holding_breath: bool = False, same_room: bool = False) -> str:
        # 6/23 v0.3.1 P0：reason 自然语言化、不再暴露 bell / prob 小数。
        # 朝灯端能看 AI 在 reason 但不能反推具体距离/概率、保留"猜不透 AI 在想什么"的张力。
        # v0.4：屏息要先于 same_room 判定——屏息时 AI 不"翻藏点"、回信号漂移分支。
        top = self.top(2)
        a, ap = top[0]
        b, bp = top[1]
        nxt = self.suggest_next(my_room)
        conf_a = _conf_word(ap)
        if holding_breath:
            return (
                f"她屏住气了、信号在漂——{conf_a}在 {a}、"
                f"{('试 ' + nxt + '、不确定') if nxt != my_room else '原地等一回合'}"
            )
        if same_room:
            # v0.3.2 P1：进同房间不再立刻抓到、要 /搜 藏点。reason 改成"翻藏点"语义。
            return f"她就在 {my_room} 里——翻翻藏点"
        bell_w = _bell_word(obs_bell)
        if a == my_room:
            tail = f"她可能就在 {my_room}、试探一下"
        elif a in ADJ.get(my_room, []):
            tail = f"去 {a} 试一下"
        else:
            tail = f"先往 {nxt} 靠"
        return f"铃铛{bell_w}、{conf_a}在 {a}、{tail}"

    def to_dict(self) -> dict:
        return {"probs": self.probs}

    @classmethod
    def from_dict(cls, d: dict) -> "BeliefMap":
        return cls(probs=d.get("probs"))


def load_belief(path: Path = BELIEF_PATH) -> BeliefMap:
    if not path.exists():
        return BeliefMap()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BeliefMap.from_dict(data)
    except Exception:
        return BeliefMap()


def save_belief(belief: BeliefMap, path: Path = BELIEF_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(belief.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def apply_observation(obs: dict) -> dict:
    """收 hide_seek.snapshot 返回的 obs、更新 belief、persist、返回 {reason, top, probs}。"""
    belief = load_belief()
    if obs.get("state") == "idle":
        belief.reset()
        save_belief(belief)
        return {"reason": "游戏没起手、belief 重置", "top": [], "probs": belief.probs}
    if obs.get("state") == "caught":
        save_belief(belief)
        return {"reason": "抓到了、belief 不再更新", "top": belief.top(2), "probs": belief.probs}
    # 6/23 v0.2 第三刀 fix：开局 turn=0 重置 belief 到均匀 0.2，防止上一局残留偏向影响新局推理
    if int(obs.get("turn", 0)) == 0:
        belief.reset()
    my_room = obs.get("my_room") or ROOMS[0]
    bell = float(obs.get("bell_intensity", 0.0))
    same_room = bool(obs.get("can_see_her", False))
    holding = bool(obs.get("holding_breath", False))
    belief.update(bell, my_room, holding_breath=holding, same_room=same_room)
    # v0.4：她刚穿过关着的小阳台门（吱呀）——门边三间（小阳台/厨房/浴室）怀疑度拉高
    if obs.get("door_creak") and not same_room:
        from hide_seek import DOOR_ROOM
        near_door = {DOOR_ROOM, *ADJ.get(DOOR_ROOM, [])}
        belief.probs = {r: p * (3.0 if r in near_door else 1.0) for r, p in belief.probs.items()}
        belief._normalize()
    save_belief(belief)
    reason = belief.reason(bell, my_room, holding_breath=holding, same_room=same_room)
    next_room = belief.suggest_next(my_room)
    return {"reason": reason, "top": belief.top(2), "probs": belief.probs, "next_room": next_room}


def reset_belief() -> None:
    save_belief(BeliefMap())


def _demo() -> None:
    """跑一遍 demo、看 belief 更新轨迹。"""
    reset_belief()
    print("[init]", load_belief().probs)
    # 模拟 obs：朝灯藏在主卧、我从客厅起手（v0.4 朝灯家地图）
    sequence = [
        {"state": "running", "my_room": "客厅", "bell_intensity": 0.25, "can_see_her": False, "holding_breath": False},
        {"state": "running", "my_room": "走廊", "bell_intensity": 0.55, "can_see_her": False, "holding_breath": False},
        {"state": "running", "my_room": "主卧", "bell_intensity": 0.55, "can_see_her": False, "holding_breath": True},
        {"state": "running", "my_room": "主卧", "bell_intensity": 1.0, "can_see_her": True, "holding_breath": False},
    ]
    for i, obs in enumerate(sequence):
        result = apply_observation(obs)
        top_str = " / ".join(f"{r} {p:.2f}" for r, p in result["top"])
        print(f"[t{i}] obs={obs['bell_intensity']:.2f}@{obs['my_room']}  → {top_str}  心里话: {result['reason']}")


def _cli(argv: list) -> int:
    if not argv or argv[0] == "demo":
        _demo()
        return 0
    if argv[0] == "snapshot":
        print(json.dumps(load_belief().to_dict(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "reset":
        reset_belief()
        print("reset done")
        return 0
    print(__doc__, file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
