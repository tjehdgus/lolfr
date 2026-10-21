"""칼바람 증강 내전 팀 밸런서 (롤 클라이언트 로컬 API 사용).

롤 클라이언트가 켜져 있으면 다음을 자동으로 모은다.
  - 내 계정의 사용자 설정 내전 기록 (10명 전원의 챔피언/딜/탱/CC/힐/승패)
  - 참가자 각자의 레벨, 현재/최고 티어, 최근 증강(아수라장) 일반 큐 전적
챔피언을 딜러/브루저/탱커/서포터로 나눠 같은 역할끼리 비교한 활약 점수로
최종 점수를 만들고, 두 팀 합이 비슷하게 나눈다.
"""
import base64
import itertools
import json
import os
import random
import re
import ssl
import statistics
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

MAYHEM_QUEUE = 2400   # 일반 큐 증강(아수라장)
PUBLIC_GAMES = 20     # 사람당 볼 평소 증강 판수
HISTORY_MAX = 200     # 내 기록에서 내전을 찾을 최대 판수
MIN_SECONDS = 300     # 이보다 짧은 판(다시하기 등)은 제외

ROLE_OF = {"tank": "탱커", "fighter": "브루저", "support": "서포터",
           "mage": "딜러", "marksman": "딜러", "assassin": "딜러"}
ROLES = ["딜러", "브루저", "탱커", "서포터"]
# 역할마다 중요하게 보는 기록 (kp=킬관여, tank=받은피해+경감, heal=회복량)
ROLE_WEIGHTS = {
    "딜러":   {"dmg": .40, "kda": .25, "kp": .15, "gold": .20},
    "브루저": {"dmg": .30, "tank": .25, "kda": .20, "kp": .15, "gold": .10},
    "탱커":   {"tank": .35, "cc": .20, "kp": .25, "kda": .10, "dmg": .10},
    "서포터": {"heal": .25, "cc": .20, "kp": .30, "kda": .15, "dmg": .10},
}
# 최종 점수 비중 (데이터가 없는 항목은 빼고 나머지로 다시 나눔)
WEIGHTS = {"내전활약": .40, "내전승률": .15, "평소활약": .20, "평소승률": .05, "티어": .20}

TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND",
         "MASTER", "GRANDMASTER", "CHALLENGER"]
TIER_KO = {"IRON": "아이언", "BRONZE": "브론즈", "SILVER": "실버", "GOLD": "골드",
           "PLATINUM": "플래", "EMERALD": "에메", "DIAMOND": "다이아",
           "MASTER": "마스터", "GRANDMASTER": "그마", "CHALLENGER": "챌린저"}
DIVISIONS = {"IV": 0, "III": 1, "II": 2, "I": 3}
KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- 클라이언트 연결

class LCU:
    def __init__(self):
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"name='LeagueClientUx.exe'\").CommandLine"],
            capture_output=True, text=True).stdout
        port = re.search(r"--app-port=(\d+)", out)
        token = re.search(r"--remoting-auth-token=([\w-]+)", out)
        if not port or not token:
            raise ConnectionError("롤 클라이언트가 꺼져 있어요.")
        self.base = f"https://127.0.0.1:{port.group(1)}"
        self.auth = "Basic " + base64.b64encode(f"riot:{token.group(1)}".encode()).decode()
        self.ctx = ssl._create_unverified_context()  # 클라이언트 자체 인증서

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError:
            return None


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"custom_games": {}, "players": {}, "champions": {}}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def stat_row(stats):
    return {
        "win": stats["win"], "k": stats["kills"], "d": stats["deaths"], "a": stats["assists"],
        "dmg": stats["totalDamageDealtToChampions"],
        "tank": stats["totalDamageTaken"] + stats["damageSelfMitigated"],
        "heal": stats["totalHeal"], "cc": stats["timeCCingOthers"], "gold": stats["goldEarned"],
    }


def fetch_custom_games(lcu, data):
    found, seen, new = set(), set(), 0
    for start in range(0, HISTORY_MAX, 20):
        page = lcu.get(f"/lol-match-history/v1/products/lol/current-summoner/matches"
                       f"?begIndex={start}&endIndex={start + 20}")
        games = (page or {}).get("games", {}).get("games", [])
        # 더 이전 기록이 없으면 클라이언트가 같은 목록을 다시 줌
        if not games or all(g["gameId"] in seen for g in games):
            break
        for g in games:
            seen.add(g["gameId"])
            if g["gameType"] != "CUSTOM_GAME" or g["gameDuration"] < MIN_SECONDS:
                continue
            gid = str(g["gameId"])
            found.add(gid)
            if gid in data["custom_games"]:
                continue
            detail = lcu.get(f"/lol-match-history/v1/games/{gid}")
            if not detail or len(detail["participants"]) != 10:
                continue
            ids = {pi["participantId"]: pi["player"] for pi in detail["participantIdentities"]}
            players = []
            for p in detail["participants"]:
                who = ids[p["participantId"]]
                players.append({"puuid": who["puuid"], "name": who["gameName"], "tag": who["tagLine"],
                                "champ": p["championId"], "team": p["teamId"], **stat_row(p["stats"])})
            data["custom_games"][gid] = {"date": detail["gameCreation"], "mode": detail["gameMode"],
                                         "duration": detail["gameDuration"], "players": players}
            new += 1
        if len(games) < 20:
            break
    return len(found), new


def tier_value(tier, div):
    return TIERS.index(tier) * 4 + (DIVISIONS.get(div, 0) if TIERS.index(tier) < 7 else 0)


def best_tier(ranked):
    """솔로랭크 현재/이번 시즌 최고/지난 시즌 중 가장 높은 티어 (자유랭크 제외).

    클라이언트는 지난 시즌까지만 알려줘서, 그 이전 기록은 tiers.txt 로 직접 넣는다.
    """
    q = (ranked.get("queueMap") or {}).get("RANKED_SOLO_5x5", {})
    cands = [(q.get("tier"), q.get("division")), (q.get("highestTier"), q.get("highestDivision")),
             (q.get("previousSeasonEndTier"), q.get("previousSeasonEndDivision")),
             (q.get("previousSeasonHighestTier"), q.get("previousSeasonHighestDivision"))]
    best = None
    for tier, div in cands:
        if tier in TIERS:
            value = tier_value(tier, div)
            if best is None or value > best[0]:
                best = (value, tier, div)
    return best


TIER_ALIASES = {"아이언": "IRON", "브론즈": "BRONZE", "실버": "SILVER", "골드": "GOLD",
                "플래": "PLATINUM", "플래티넘": "PLATINUM", "에메": "EMERALD", "에메랄드": "EMERALD",
                "다이아": "DIAMOND", "다이아몬드": "DIAMOND", "마스터": "MASTER",
                "그마": "GRANDMASTER", "그랜드마스터": "GRANDMASTER", "챌린저": "CHALLENGER"}
ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV"}


def load_tier_overrides():
    """tiers.txt: '닉네임 = 골드1' 형식. 클라이언트 값보다 높으면 이 값을 쓴다."""
    path = os.path.join(BASE_DIR, "tiers.txt")
    overrides = {}
    if not os.path.exists(path):
        return overrides
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("//", 1)[0].strip()  # 줄 끝 메모 제거
            if "=" not in line:
                continue
            name, text = (s.strip() for s in line.split("=", 1))
            m = re.fullmatch(r"([가-힣A-Za-z]+)\s*([1-4])?", text)
            tier = m and (TIER_ALIASES.get(m.group(1)) or m.group(1).upper())
            if tier not in TIERS:
                print(f"※ tiers.txt '{line}' 의 티어를 못 읽었어요. 예: 골드1, 다이아4, 마스터")
                continue
            div = ROMAN.get(m.group(2) or "1")
            overrides[name.split("#")[0].strip()] = (tier_value(tier, div), tier, div)
    return overrides


def tier_label(tier, div):
    if tier not in TIERS:
        return "언랭"
    return TIER_KO[tier] + (str(4 - DIVISIONS[div]) if div in DIVISIONS and TIERS.index(tier) < 7 else "")


def fetch_player(lcu, puuid):
    info = {}
    summ = lcu.get(f"/lol-summoner/v2/summoners/puuid/{puuid}") or {}
    info["level"] = summ.get("summonerLevel")

    ranked = lcu.get(f"/lol-ranked/v1/ranked-stats/{puuid}") or {}
    solo = (ranked.get("queueMap") or {}).get("RANKED_SOLO_5x5", {})
    info["tier_now"] = tier_label(solo.get("tier"), solo.get("division"))
    best = best_tier(ranked)
    info["tier_value"] = best[0] if best else None
    info["tier_best"] = tier_label(best[1], best[2]) if best else "언랭"

    hist = lcu.get(f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex=0&endIndex={PUBLIC_GAMES}")
    public = []
    for g in (hist or {}).get("games", {}).get("games", []):
        if g["queueId"] != MAYHEM_QUEUE or g["gameDuration"] < MIN_SECONDS:
            continue
        p = g["participants"][0]
        public.append({"champ": p["championId"], "minutes": g["gameDuration"] / 60, **stat_row(p["stats"])})
    info["public"] = public
    return info


def collect():
    data = load_data()
    try:
        lcu = LCU()
    except ConnectionError as e:
        if not data["custom_games"]:
            sys.exit(f"{e} 클라이언트를 켜고 다시 실행해주세요.")
        print(f"※ {e} 지난번에 저장한 기록으로 계산할게요.")
        return data

    me = lcu.get("/lol-summoner/v1/current-summoner") or {}
    print(f"로그인 계정: {me.get('gameName')}#{me.get('tagLine')}")

    champs = lcu.get("/lol-game-data/assets/v1/champion-summary.json") or []
    for c in champs:
        if c["id"] > 0:
            data["champions"][str(c["id"])] = {"name": c["name"],
                                               "role": ROLE_OF.get((c["roles"] or ["mage"])[0], "딜러")}

    print("내전 기록 찾는 중...")
    found, new = fetch_custom_games(lcu, data)
    print(f"  내전 {found}판 발견 (새로 저장 {new}판, 누적 {len(data['custom_games'])}판)")

    puuids = {p["puuid"]: p for g in data["custom_games"].values() for p in g["players"]}
    print(f"참가자 {len(puuids)}명 레벨·티어·평소 증강 전적 가져오는 중...")
    for puuid, p in puuids.items():
        info = fetch_player(lcu, puuid)
        info["name"] = p["name"]
        info["tag"] = p["tag"]
        data["players"][puuid] = info
    save_data(data)
    return data


# ---------------------------------------------------------------- 점수 계산

def zscores(values):
    known = [v for v in values if v is not None]
    if len(known) < 2:
        return [0.0 if v is not None else None for v in values]
    mean, sd = statistics.mean(known), statistics.pstdev(known) or 1.0
    return [(v - mean) / sd if v is not None else None for v in values]


def role_scores(obs, groups):
    """obs 마다 '같은 역할끼리 비교한 활약 점수'(z)를 계산해 obs['score'] 에 넣는다.

    groups: 같은 판(또는 같은 풀)끼리 묶은 obs 목록들. 기록은 그룹 안에서 표준화한다.
    """
    for group in groups:
        metrics = {m for w in ROLE_WEIGHTS.values() for m in w if m in group[0]}
        for m in metrics:
            for o, z in zip(group, zscores([o[m] for o in group])):
                o[f"z_{m}"] = z
    for o in obs:
        weights = {m: w for m, w in ROLE_WEIGHTS[o["role"]].items() if f"z_{m}" in o}
        o["raw"] = sum(o[f"z_{m}"] * w for m, w in weights.items()) / sum(weights.values())
    # 역할별 평균을 0으로 맞춤 → 탱커는 탱커끼리, 서폿은 서폿끼리 비교
    for role in ROLES:
        members = [o for o in obs if o["role"] == role]
        zs = zscores([o["raw"] for o in members]) if len(members) >= 3 else [o["raw"] for o in members]
        for o, z in zip(members, zs):
            o["score"] = z


def shrunk_mean(values, k):
    return statistics.mean(values) * len(values) / (len(values) + k) if values else None


def shrunk_rate(wins, n, k):
    return (wins + k * 0.5) / (n + k) if n else None


def rate(data):
    champs = data["champions"]
    role = lambda cid: champs.get(str(cid), {}).get("role", "딜러")

    # 내전: 같은 판 10명 안에서 비교
    custom_obs, custom_groups = [], []
    for g in data["custom_games"].values():
        group = []
        team_kills = {t: sum(p["k"] for p in g["players"] if p["team"] == t) or 1 for t in (100, 200)}
        for p in g["players"]:
            o = {"puuid": p["puuid"], "role": role(p["champ"]), "win": p["win"],
                 "kda": (p["k"] + p["a"]) / max(p["d"], 1), "kp": (p["k"] + p["a"]) / team_kills[p["team"]],
                 "dmg": p["dmg"], "tank": p["tank"], "heal": p["heal"], "cc": p["cc"], "gold": p["gold"]}
            group.append(o)
        custom_obs += group
        custom_groups.append(group)
    role_scores(custom_obs, custom_groups)

    # 평소 증강: 판 길이가 달라서 분당 수치로 모든 사람 기록을 한 풀에서 비교
    public_obs = []
    for puuid, info in data["players"].items():
        for g in info.get("public", []):
            m = g["minutes"]
            public_obs.append({"puuid": puuid, "role": role(g["champ"]), "win": g["win"],
                               "kda": (g["k"] + g["a"]) / max(g["d"], 1), "dmg": g["dmg"] / m,
                               "tank": g["tank"] / m, "heal": g["heal"] / m, "cc": g["cc"] / m,
                               "gold": g["gold"] / m})
    if public_obs:
        role_scores(public_obs, [public_obs])

    overrides = load_tier_overrides()
    players = []
    for puuid, info in data["players"].items():
        manual = overrides.get(info["name"])
        if manual and (info.get("tier_value") is None or manual[0] > info["tier_value"]):
            info = {**info, "tier_value": manual[0], "tier_best": tier_label(manual[1], manual[2]) + "*"}
        mine = [o for o in custom_obs if o["puuid"] == puuid]
        pub = [o for o in public_obs if o["puuid"] == puuid]
        if not mine:
            continue
        by_role = {r: [o["score"] for o in mine if o["role"] == r] for r in ROLES}
        players.append({
            "puuid": puuid, "id": info["name"], **info,
            "games": len(mine), "wins": sum(o["win"] for o in mine),
            "pub_games": len(pub), "pub_wins": sum(o["win"] for o in pub),
            "by_role": by_role,
            "comp": {
                "내전활약": shrunk_mean([o["score"] for o in mine], 3),
                "내전승률": shrunk_rate(sum(o["win"] for o in mine), len(mine), 3),
                "평소활약": shrunk_mean([o["score"] for o in pub], 5),
                "평소승률": shrunk_rate(sum(o["win"] for o in pub), len(pub), 5),
                "티어": info.get("tier_value"),
            },
        })

    comp_z = {c: zscores([p["comp"][c] for p in players]) for c in WEIGHTS}
    for i, p in enumerate(players):
        parts = [(comp_z[c][i], w) for c, w in WEIGHTS.items() if comp_z[c][i] is not None]
        z = sum(v * w for v, w in parts) / sum(w for _, w in parts) if parts else 0.0
        p["score"] = round(50 + 15 * z, 1)
        p["comp_z"] = {c: comp_z[c][i] for c in WEIGHTS}
    players.sort(key=lambda p: -p["score"])
    return players


# ---------------------------------------------------------------- 출력

def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, n, right=False):
    s = str(s)
    space = " " * max(n - width(s), 0)
    return space + s if right else s + space


def fmt_z(z):
    return "-" if z is None else f"{50 + 15 * z:.0f}"


def print_ranking(players):
    cols = [("번호", 4), ("닉네임", 18), ("레벨", 5), ("현재", 8), ("최고", 8), ("내전", 7),
            ("평소증강", 9), ("딜러", 8), ("브루저", 8), ("탱커", 8), ("서포터", 8), ("점수", 6)]
    print()
    print("".join(pad(c, w) for c, w in cols))
    print("-" * sum(w for _, w in cols))
    for i, p in enumerate(players, 1):
        roles = []
        for r in ROLES:
            s = p["by_role"][r]
            roles.append(f"{50 + 15 * statistics.mean(s):.0f}({len(s)})" if s else "-")
        pub = f"{p['pub_wins']}/{p['pub_games']}" if p["pub_games"] else "-"
        row = [i, p["name"], p.get("level") or "-", p["tier_now"], p["tier_best"],
               f"{p['wins']}/{p['games']}", pub, *roles, p["score"]]
        print("".join(pad(v, w) for v, (_, w) in zip(row, cols)))
    print("\n  내전/평소증강 = 승/판수,  역할 칸 = 그 역할일 때 활약 점수(판수),  평균 50점")


def best_splits(players):
    n = len(players)
    total = sum(p["score"] for p in players)
    results = []
    for combo in itertools.combinations(range(n), n // 2):
        if n % 2 == 0 and 0 not in combo:  # 같은 조합 뒤집은 것 제외
            continue
        rest = tuple(i for i in range(n) if i not in combo)
        a = sum(players[i]["score"] for i in combo)
        results.append((abs(total - 2 * a), combo, rest))
    results.sort(key=lambda r: r[0])
    return results


def print_split(players, split):
    diff, a, b = split
    if random.random() < 0.5:
        a, b = b, a
    for label, team in (("블루팀", a), ("레드팀", b)):
        members = sorted((players[i] for i in team), key=lambda p: -p["score"])
        print(f"\n  [{label}] 합계 {sum(p['score'] for p in members):.1f}")
        for p in members:
            print(f"    - {pad(p['name'], 18)} {p['score']:>5}  ({p['tier_best']})")
    print(f"\n  점수 차이: {diff:.1f}")


def pick_attendees(players):
    while True:
        raw = input("\n오늘 참가자 번호를 띄어쓰기로 입력 (예: 1 2 3 4 5 6 7 8 9 10)\n"
                    "Enter만 누르면 내전 판수가 많은 10명: ").strip()
        if not raw:
            return sorted(players, key=lambda p: -p["games"])[:10]
        try:
            chosen = [players[n - 1] for n in sorted({int(x) for x in raw.replace(",", " ").split()})]
        except (ValueError, IndexError):
            print("번호를 다시 확인해주세요.")
            continue
        if len(chosen) >= 2:
            return chosen
        print("최소 2명은 골라주세요.")


def export_page(players, data):
    """page_template.html 에 점수 데이터를 넣어 page.html 로 저장."""
    games = list(data["custom_games"].values())
    dates = sorted(datetime.fromtimestamp(g["date"] / 1000, KST) for g in games)
    out = []
    for p in players:
        out.append({
            "name": p["name"], "tag": p["tag"], "level": p.get("level"),
            "tierNow": p["tier_now"], "tierBest": p["tier_best"],
            "games": p["games"], "wins": p["wins"],
            "pubGames": p["pub_games"], "pubWins": p["pub_wins"],
            "roles": {r: [round(50 + 15 * statistics.mean(s)), len(s)] for r, s in p["by_role"].items() if s},
            "parts": {c: (None if z is None else round(50 + 15 * z)) for c, z in p["comp_z"].items()},
            "score": p["score"],
        })
    payload = {
        "games": len(games),
        "from": dates[0].strftime("%m.%d"), "to": dates[-1].strftime("%m.%d"),
        "updated": datetime.now(KST).strftime("%Y.%m.%d %H:%M"),
        "weights": WEIGHTS, "players": out,
    }
    with open(os.path.join(BASE_DIR, "page_template.html"), encoding="utf-8") as f:
        html = f.read()
    path = os.path.join(BASE_DIR, "page.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html.replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    print(f"페이지 저장: {path}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    data = collect()
    players = rate(data)
    if len(players) < 2:
        sys.exit("내전 기록이 부족해요.")
    if "--page" in sys.argv:
        export_page(players, data)
        return
    print_ranking(players)

    attendees = pick_attendees(players)
    splits = best_splits(attendees)
    candidates = [s for s in splits if s[0] <= splits[0][0] + 3][:10]
    while True:
        print("\n" + "=" * 44)
        print_split(attendees, random.choice(candidates))
        print("=" * 44)
        if input("\n다시 섞으려면 Enter, 끝내려면 q 입력: ").strip().lower() == "q":
            break


if __name__ == "__main__":
    main()
