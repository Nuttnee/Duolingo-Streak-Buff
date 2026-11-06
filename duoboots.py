import time
import json
import re
import os
import sys
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests

def green(t): return f"\033[92m{t}\033[0m"
def cyan(t): return f"\033[96m{t}\033[0m"
def gold(t): return f"\033[93m{t}\033[0m"
def red(t): return f"\033[91m{t}\033[0m"

import threading

class ProxyPool:
    def __init__(self, proxy_source=None):
        self.proxies = []
        self._index = 0
        self._lock = threading.Lock()
        if proxy_source:
            if isinstance(proxy_source, list):
                for p in proxy_source:
                    fmt = self._format_proxy(p)
                    if fmt: self.proxies.append(fmt)
            elif isinstance(proxy_source, str) and os.path.exists(proxy_source):
                with open(proxy_source, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            fmt = self._format_proxy(line)
                            if fmt: self.proxies.append(fmt)
            elif isinstance(proxy_source, str):
                fmt = self._format_proxy(proxy_source)
                if fmt: self.proxies.append(fmt)

    def _format_proxy(self, p_str):
        p_str = p_str.strip()
        if not p_str: return None
        if p_str.startswith("http://") or p_str.startswith("https://") or p_str.startswith("socks5://"):
            return p_str
        parts = p_str.split(":")
        if len(parts) == 4:
            return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        elif len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        return f"http://{p_str}"

    def get_proxy(self):
        if not self.proxies:
            return None
        with self._lock:
            p = self.proxies[self._index % len(self.proxies)]
            self._index += 1
            return p

    def has_proxies(self):
        return len(self.proxies) > 0

class DuoBooster:
    def __init__(self, identifier, password, proxy=None):
        self.identifier = identifier
        self.password = password
        self.proxy_pool = ProxyPool(proxy) if not isinstance(proxy, ProxyPool) else proxy
        self.proxy = self.proxy_pool.get_proxy() if self.proxy_pool.has_proxies() else (proxy if isinstance(proxy, str) else None)
        self.headers = {
            "User-Agent": "Duodroid/5.141.11 Dalvik/2.1.0 (Linux; U; Android 9; SM-S9280 Build/PQ3A.190705.08272029)",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.jwt = None
        self.uid = None
        self.from_lang = "en"
        self.learning_lang = "es"
        self.local = threading.local()
        self.session_template = None
        self.base_sid = None
        self.template_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.story_lock = threading.Lock()
        self.story_seq = 0
        self.last_req_time = 0.0
        self.proxy_alive = True
        self.stop_requested = threading.Event()
        # Main session for profile queries
        self.session = self._create_session()

    def stop(self):
        self.stop_requested.set()

    def _create_session(self, use_proxy=None):
        if use_proxy is None:
            use_proxy = self.proxy_alive
        curr_p = self.proxy_pool.get_proxy() if self.proxy_pool.has_proxies() else self.proxy
        p = {'http': curr_p, 'https': curr_p} if (curr_p and use_proxy) else None
        s = requests.Session(
            impersonate="chrome124",
            proxies=p
        )
        s.headers.update(self.headers)
        if self.jwt:
            s.headers["Authorization"] = f"Bearer {self.jwt}"
        return s

    def get_worker_session(self, reset=False):
        # Each worker thread gets its own isolated curl session with automatic proxy rotation
        if reset or not hasattr(self.local, "session"):
            self.local.session = self._create_session()
        return self.local.session

    def login(self):
        cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_cache.json")
        cache_key = self.identifier.strip().lower()
        cache_db = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache_db = json.load(f) or {}
            except Exception:
                cache_db = {}

        login_url = "https://android-api-cf.duolingo.com/2017-06-30/login?fields=id,username,gems,totalXp,streak,fromLanguage,learningLanguage"
        payload = {"identifier": self.identifier, "password": self.password}
        res = None
        last_err = ""
        for attempt in range(4):
            try:
                res = self.session.post(login_url, json=payload, timeout=15)
                if res.status_code == 200 and res.headers.get("jwt"):
                    break
                if res.status_code >= 500 or "Auth Error" in res.text:
                    time.sleep(1.2)
                    continue
            except Exception as e:
                last_err = str(e)
                if attempt >= 1:
                    self.proxy_alive = False
                    self.session = self._create_session(use_proxy=False)
                time.sleep(0.5)

        if res is not None and res.status_code == 200 and res.headers.get("jwt"):
            self.jwt = res.headers.get("jwt")
            data = res.json()
            self.uid = data.get("id")
            if data.get("fromLanguage"):
                self.from_lang = data["fromLanguage"]
            if data.get("learningLanguage"):
                self.learning_lang = data["learningLanguage"]
            self.session.headers.update({"Authorization": f"Bearer {self.jwt}"})
            try:
                cache_db[cache_key] = {
                    "jwt": self.jwt,
                    "uid": self.uid,
                    "fromLanguage": self.from_lang,
                    "learningLanguage": self.learning_lang,
                    "data": data,
                }
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache_db, f)
            except Exception:
                pass
            return True, data

        # Fallback sang JWT Cache nếu Auth Microservice của Duolingo bị quá tải tạm thời
        if cache_key in cache_db and cache_db[cache_key].get("jwt") and cache_db[cache_key].get("uid"):
            c_entry = cache_db[cache_key]
            self.jwt = c_entry["jwt"]
            self.uid = c_entry["uid"]
            self.from_lang = c_entry.get("fromLanguage", self.from_lang or "en")
            self.learning_lang = c_entry.get("learningLanguage", self.learning_lang or "es")
            self.session.headers.update({"Authorization": f"Bearer {self.jwt}"})
            return True, (c_entry.get("data") or {"id": self.uid, "fromLanguage": self.from_lang, "learningLanguage": self.learning_lang})

        return False, (res.text if res is not None else last_err)

    def register(self, email=None, password=None, name=None):
        target_email = email or self.identifier
        target_pass = password or self.password
        if "@" not in str(target_email):
            return False, "Identifier is not an email"

        name_val = name or str(target_email).split("@")[0]
        clean_name = re.sub(r"[^a-zA-Z0-9_]", "", name_val)[:18] or "DuoLearner"

        reg_payload = {
            "age": 25,
            "email": target_email,
            "name": clean_name,
            "password": target_pass,
            "fromLanguage": self.from_lang or "en",
            "learningLanguage": self.learning_lang or "es",
            "timezone": "Asia/Ho_Chi_Minh"
        }

        url = "https://android-api-cf.duolingo.com/2017-06-30/users?fields=id,username"
        for attempt in range(3):
            try:
                r = self.session.post(url, json=reg_payload, timeout=12)
                if r.status_code == 200 and r.headers.get("jwt"):
                    self.jwt = r.headers.get("jwt")
                    data = r.json()
                    self.uid = data.get("id")
                    self.session.headers.update({"Authorization": f"Bearer {self.jwt}"})
                    return True, data
                elif r.status_code == 400:
                    err_data = r.json() if r.text else {}
                    if "EMAIL_TAKEN" in str(err_data):
                        return False, "EMAIL_TAKEN"
                    return False, str(err_data)
                elif r.status_code == 422:
                    return False, f"Schema error: {r.text[:100]}"
            except Exception as e:
                if attempt == 2:
                    return False, str(e)
                time.sleep(0.5)
        return False, "Failed to register after retries"

    def get_info(self):
        urls = [
            f"https://android-api-cf.duolingo.com/2017-06-30/users/{self.uid}?fields=streak,streakData,totalXp,gemsConfig,gems,lingots,username,fromLanguage,learningLanguage,hasPlus",
            f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=streak,streakData,totalXp,gemsConfig,gems,lingots,username,fromLanguage,learningLanguage,hasPlus",
        ]
        for attempt in range(3):
            for u in urls:
                try:
                    res = self.session.get(u, headers=self._get_web_headers() if "www.duolingo.com" in u else None, timeout=10)
                    if res.status_code == 200:
                        data = res.json()
                        if data.get("fromLanguage"):
                            self.from_lang = data["fromLanguage"]
                        if data.get("learningLanguage"):
                            self.learning_lang = data["learningLanguage"]
                        return data
                except Exception:
                    pass
            if attempt == 1:
                self.session = self._create_session(use_proxy=False)
            time.sleep(0.3)
        return None

    def get_session_template(self):
        if self.session_template is not None:
            return self.session_template
        with self.template_lock:
            if self.session_template is not None:
                return self.session_template
            session_create_url = "https://android-api-cf.duolingo.com/2017-06-30/sessions"
            req_data = {
                "challengeTypes": ["translate"],
                "fromLanguage": self.from_lang or "en",
                "learningLanguage": self.learning_lang or "es",
                "type": "GLOBAL_PRACTICE"
            }
            for attempt in range(4):
                try:
                    r_create = self.session.post(session_create_url, json=req_data, timeout=12)
                    if r_create.status_code == 200:
                        sd = r_create.json()
                        sid = sd.get("id", "zsWoDn8ldl000000")
                        self.base_sid = sid[:-6] if len(sid) > 6 else sid
                        # Strip ~415 KB of challenge/audio bloat -> leaves ~3.8 KB ultra-slim payload
                        slim_sd = {
                            k: v for k, v in sd.items()
                            if k not in (
                                "challenges",
                                "adaptiveChallenges",
                                "adaptiveInterleavedChallenges",
                                "mistakesReplacementChallenges",
                                "sessionElementSolutions",
                                "ttsAnnotations"
                            )
                        }
                        slim_sd["challenges"] = []
                        slim_sd["adaptiveChallenges"] = []
                        slim_sd["sessionElementSolutions"] = []
                        self.session_template = slim_sd
                        return self.session_template
                    elif r_create.status_code == 429:
                        time.sleep(0.5 * (attempt + 1))
                except Exception:
                    if attempt == 2:
                        self.session = self._create_session(use_proxy=False)
                    time.sleep(0.3)
            return None

    def _pace_request(self, min_interval=0.14):
        with self.rate_lock:
            now_t = time.time()
            wait = max(0.0, self.last_req_time + min_interval - now_t)
            self.last_req_time = now_t + wait
        if wait > 0:
            time.sleep(wait)

    def solve_session(self, day_offset=0, is_xp_farm=False, target_ts=None):
        if target_ts is not None:
            target_timestamp = int(target_ts)
        else:
            target_timestamp = int(time.time()) - (day_offset * 86400)

        tmpl = self.get_session_template()
        if not tmpl:
            return day_offset, False, "Template init failed"

        fake_sid = self.base_sid + "".join(random.choices(string.ascii_letters + string.digits, k=6))
        # Human-like randomized lesson duration (65 - 145 seconds) & realistic heart distribution
        duration = random.randint(65, 145)
        simulated_hearts = random.choice([4, 5, 5, 5])
        submit_payload = {
            **tmpl,
            "id": fake_sid,
            "failed": False,
            "heartsLeft": simulated_hearts,
            "startTime": target_timestamp - duration,
            "endTime": target_timestamp,
            "hasBoost": True,
            "sessionElementSolutions": []
        }
        put_url = f"https://android-api-cf.duolingo.com/2017-06-30/sessions/{fake_sid}"
        sess = self.get_worker_session()

        for attempt in range(8):
            try:
                self._pace_request(0.12 + random.uniform(0.01, 0.05))
                r_sub = sess.put(put_url, json=submit_payload, timeout=8)
                if r_sub.status_code == 200:
                    gain = r_sub.json().get("xpGain", 26)
                    return day_offset, True, gain
                if r_sub.status_code == 429:
                    retry_after = r_sub.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        backoff = float(retry_after) + random.uniform(0.1, 0.5)
                    else:
                        backoff = (0.25 * (1.5 ** attempt)) + random.uniform(0.05, 0.2)
                    time.sleep(backoff)
                    if attempt >= 2 and self.proxy_pool.has_proxies():
                        sess = self.get_worker_session(reset=True)
                    continue
                if r_sub.status_code >= 500:
                    time.sleep(0.2 + random.uniform(0.05, 0.15))
                    continue
                return day_offset, False, f"Submit error: {r_sub.status_code}"
            except Exception as e:
                sess = self.get_worker_session(reset=True)
                if attempt == 7:
                    return day_offset, False, str(e)
        return day_offset, False, "Max retries"

    def boost_streak(self, add_days=100, workers=15, batch_size=50):
        info = self.get_info()
        if not info:
            print(red("[-] Không thể lấy thông tin streak từ server."))
            return

        current_streak = info.get("streak", 0)
        streak_data = info.get("streakData", {}) or {}
        cur_obj = streak_data.get("currentStreak") or {}
        target_days = current_streak + add_days

        print(gold(f"[+] Streak hiện tại: {current_streak} ngày"))
        if cur_obj.get("startDate") and cur_obj.get("endDate"):
            print(gold(f"[+] Khoảng thời gian: {cur_obj.get('startDate')} -> {cur_obj.get('endDate')}"))

        if add_days <= 0:
            print(gold("[!] Số ngày bơm thêm <= 0, bỏ qua."))
            return

        # Pre-warm ultra-slim session template once (3.8 KB instead of 418 KB per day)
        print(cyan("[*] Đang khởi tạo Ultra-Slim Session Template (1-PUT/ngày, giảm 99% băng thông)..."))
        if not self.get_session_template():
            print(red("[-] Không thể khởi tạo Session Template."))
            return

        # If streak is 0, anchor today first so startTimestamp is established on server
        if current_streak == 0 or not streak_data.get("startTimestamp"):
            self.solve_session(target_ts=int(time.time()))
            info = self.get_info() or info
            current_streak = info.get("streak", 0)
            streak_data = info.get("streakData", {}) or {}
            cur_obj = streak_data.get("currentStreak") or {}

        effective_workers = min(max(workers, 4), 16)
        print(cyan(f"[*] Bắt đầu BƠM SIÊU TỐC +{add_days} ngày (Mục tiêu: {target_days} ngày) [Batch: {batch_size}, Luồng tối ưu: {effective_workers}]..."))

        start_boost_time = time.time()
        initial_streak = current_streak

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            while current_streak < target_days:
                if self.stop_requested.is_set():
                    print(gold("\n[!] Đã nhận tín hiệu dừng Bơm Streak."))
                    break
                rem_days = target_days - current_streak
                cur_batch_len = min(batch_size, rem_days)

                # Always anchor directly from server's startTimestamp to prevent duplicate-day collisions after streak merges!
                anchor_ts = streak_data.get("startTimestamp")
                if not anchor_ts:
                    anchor_ts = int(time.time()) - (max(0, current_streak - 1) * 86400)

                start_date_str = cur_obj.get("startDate", "N/A")
                print(cyan(f"[*] Đang đẩy batch {cur_batch_len} ngày lùi từ mốc {start_date_str}..."))

                batch_t0 = time.time()
                offsets = list(range(1, cur_batch_len + 1))
                futures = {
                    executor.submit(self.solve_session, idx, False, anchor_ts - (idx * 86400)): idx
                    for idx in offsets
                }

                done_cnt = 0
                failed_indices = []
                for f in as_completed(futures):
                    idx = futures[f]
                    _, ok, _ = f.result()
                    done_cnt += 1
                    if not ok:
                        failed_indices.append(idx)
                    elapsed_b = max(0.1, time.time() - batch_t0)
                    rate_b = done_cnt / elapsed_b
                    sys.stdout.write(
                        f"\r\033[96m    ➔ Đang bắn: {done_cnt}/{cur_batch_len} ngày | Tốc độ: {rate_b:.1f} ngày/s | Lỗi chờ bù: {len(failed_indices)}\033[0m"
                    )
                    sys.stdout.flush()
                print()

                # Parallel retry for any failed days in this batch
                if failed_indices:
                    retry_futs = [
                        executor.submit(self.solve_session, idx, False, anchor_ts - (idx * 86400))
                        for idx in failed_indices
                    ]
                    for rf in as_completed(retry_futs):
                        rf.result()

                new_info = self.get_info()
                if not new_info:
                    break
                new_streak = new_info.get("streak", 0)
                streak_data = new_info.get("streakData", {}) or {}
                cur_obj = streak_data.get("currentStreak") or {}
                total_elapsed = max(0.1, time.time() - start_boost_time)
                avg_speed = max(0, new_streak - initial_streak) / total_elapsed
                print(green(f"[+] Tiến độ: Streak đạt {new_streak}/{target_days} ngày (Mốc đầu: {cur_obj.get('startDate')}) | TB: {avg_speed:.1f} ngày/s"))

                if new_streak <= current_streak:
                    print(gold("[!] Phát hiện ngày bị đứt quãng sát mốc đầu, đang lấp nhanh 5 ngày biên..."))
                    edge_futs = [
                        executor.submit(self.solve_session, idx, False, anchor_ts - (idx * 86400))
                        for idx in range(1, 6)
                    ]
                    for ef in as_completed(edge_futs):
                        ef.result()
                    new_info = self.get_info() or new_info
                    new_streak = new_info.get("streak", 0)
                    streak_data = new_info.get("streakData", {}) or {}
                    cur_obj = streak_data.get("currentStreak") or {}
                    if new_streak <= current_streak:
                        print(red("[-] Server không ghi nhận thêm ngày mới. Tạm dừng."))
                        break
                current_streak = new_streak

        self.print_summary()

    def solve_story_xp(self, bonus_xp=469):
        sess = self.get_worker_session()
        end_ts = int(time.time())
        start_ts = end_ts - random.randint(60, 180)
        web_headers = {
            "authorization": f"Bearer {self.jwt}",
            "cookie": f"jwt_token={self.jwt}",
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "origin": "https://www.duolingo.com",
            "referer": "https://www.duolingo.com/learn",
            "accept": "application/json"
        }
        story_map = {
            "es": ("es-en-buenos-dias", "en", "es"),
            "fr": ("fr-en-le-passeport", "en", "fr"),
            "de": ("de-en-guten-morgen", "en", "de"),
            "it": ("it-en-buongiorno", "en", "it"),
            "pt": ("pt-en-bom-dia", "en", "pt"),
            "ja": ("ja-en-good-morning", "en", "ja"),
        }
        slug, f_lang, l_lang = story_map.get(self.learning_lang, ("es-en-buenos-dias", "en", "es"))
        payload = {
            "awardXp": True,
            "completedBonusChallenge": True,
            "fromLanguage": f_lang,
            "learningLanguage": l_lang,
            "hasXpBoost": False,
            "illustrationFormat": "svg",
            "isFeaturedStoryInPracticeHub": True,
            "isLegendaryMode": True,
            "isV2Redo": False,
            "isV2Story": False,
            "masterVersion": True,
            "maxScore": 0,
            "score": 0,
            "happyHourBonusXp": max(0, min(469, int(bonus_xp))),
            "startTime": start_ts,
            "endTime": end_ts
        }
        story_url = f"https://stories.duolingo.com/api2/stories/{slug}/complete"
        for attempt in range(6):
            try:
                self._pace_request(0.22)
                r = sess.post(story_url, headers=web_headers, json=payload, timeout=8)
                if r.status_code == 200:
                    gain = min(499, r.json().get("awardedXp", 0))
                    if gain > 0:
                        return True, gain
                elif r.status_code == 429:
                    time.sleep(0.35 + 0.15 * attempt)
                    continue
                elif slug != "es-en-buenos-dias":
                    slug = "es-en-buenos-dias"
                    payload["fromLanguage"] = "en"
                    payload["learningLanguage"] = "es"
                    story_url = f"https://stories.duolingo.com/api2/stories/{slug}/complete"
            except Exception:
                sess = self.get_worker_session(reset=True)
                time.sleep(0.15)
        # Fallback sang Ultra-Slim Session nếu Story bị chặn
        _, ok, gain = self.solve_session(0, True)
        return ok, (gain if ok else 0)

    def boost_xp(self, add_xp=2000, workers=15, batch_size=50, with_gems=True):
        info = self.get_info()
        if not info:
            print(red("[-] Không thể lấy thông tin từ server."))
            return

        initial_xp = info.get("totalXp", 0)
        current_xp = initial_xp
        target_xp = initial_xp + add_xp
        initial_gems = info.get("gems", info.get("gemsConfig", {}).get("gems", 0))
        current_gems = initial_gems

        print(gold(f"[+] XP hiện tại: {initial_xp} | Gems hiện tại: {current_gems}"))
        if with_gems:
            print(cyan(f"[*] Bắt đầu VỪA BUFF XP (+{add_xp}) VỪA BUFF GEMS (Mục tiêu: {target_xp} XP)..."))
        else:
            print(cyan(f"[*] Bắt đầu CÀY XP SIÊU TỐC (+{add_xp} XP ➔ Mục tiêu: {target_xp} XP)..."))

        needed_xp = add_xp
        if needed_xp <= 0:
            print(gold("[!] Số XP cần cày <= 0, bỏ qua."))
            return

        # Sử dụng Stories API (499 XP/hit thay vì 26 XP/hit -> nhanh gấp 20 lần!)
        xp_per_hit = 499
        hits_needed = max(1, (needed_xp + xp_per_hit - 1) // xp_per_hit)
        effective_workers = min(max(workers, 4), 8)
        effective_batch = min(max(batch_size, 10), 100)
        print(cyan(f"[*] Chế độ Turbo Stories (~499 XP/hit): Chỉ cần ~{hits_needed} hits [Luồng tối ưu: {effective_workers}, Batch: {effective_batch}]..."))

        completed_hits = 0
        xp_gained = 0
        gems_gained = 0
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            while xp_gained < needed_xp:
                if self.stop_requested.is_set():
                    print(gold("\n[!] Đã nhận tín hiệu dừng Cày XP."))
                    break
                rem_xp = needed_xp - xp_gained
                rem_hits = max(1, (rem_xp + xp_per_hit - 1) // xp_per_hit)
                current_batch = min(effective_batch, rem_hits)

                futures = []
                for i in range(current_batch):
                    if i == current_batch - 1 and rem_xp < xp_per_hit:
                        bonus = max(0, rem_xp - 30)
                    else:
                        bonus = 469
                    futures.append(executor.submit(self.solve_story_xp, bonus))

                for f in as_completed(futures):
                    ok, gain = f.result()
                    if ok and gain > 0:
                        completed_hits += 1
                        xp_gained += gain
                    elapsed = max(0.1, time.time() - start_time)
                    rate = int(xp_gained / elapsed)
                    now_xp = initial_xp + xp_gained
                    est_total_hits = max(completed_hits, completed_hits + max(0, (needed_xp - xp_gained + xp_per_hit - 1) // xp_per_hit))
                    sys.stdout.write(
                        f"\r\033[K\033[92m    ➔ Đang cày: {completed_hits}/{est_total_hits} hits | XP: {now_xp} (+{xp_gained}/{needed_xp}) | Tốc độ: ~{rate} XP/s (~{rate * 60} XP/phút)\033[0m"
                    )
                    sys.stdout.flush()
                print()

                # Đồng bộ lại tổng XP thực tế trên Server sau mỗi Batch để không bao giờ bị hụt XP ảo
                chk_info = self.get_info()
                if chk_info and "totalXp" in chk_info:
                    real_xp = chk_info.get("totalXp", initial_xp + xp_gained)
                    xp_gained = max(0, real_xp - initial_xp)

                if xp_gained >= needed_xp:
                    break

        if with_gems:
            g_gain = self.claim_gems_silent(rounds=4)
            if g_gain:
                current_gems += g_gain
                gems_gained += g_gain
                print(green(f"[+] Thu hoạch thêm +{g_gain} Gems (Tổng Gems: {current_gems})"))

        final_info = self.get_info()
        final_xp = final_info.get("totalXp", initial_xp + xp_gained) if final_info else initial_xp + xp_gained
        actual_xp_gained = max(0, final_xp - initial_xp)
        final_gems = final_info.get("gems", final_info.get("gemsConfig", {}).get("gems", current_gems)) if final_info else current_gems
        total_g_gained = final_gems - initial_gems
        elapsed_total = max(0.1, time.time() - start_time)
        print(green(f"[✓] Hoàn tất trong {elapsed_total:.1f}s! XP: {final_xp} (+{actual_xp_gained}) | Gems: {final_gems} (+{total_g_gained}) | TB: ~{int(actual_xp_gained / elapsed_total)} XP/s"))

    def _get_web_headers(self):
        return {
            "authorization": f"Bearer {self.jwt}",
            "cookie": f"jwt_token={self.jwt}",
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "device-platform": "web",
            "x-duolingo-device-platform": "web",
            "x-duolingo-app-version": "1.0.0",
            "x-duolingo-application": "chrome",
            "x-duolingo-client-version": "web",
            "accept": "application/json"
        }

    REPEATABLE_GEM_BUNDLES = {
        "SKILL_COMPLETION",
        "SKILL_COMPLETION_BALANCED",
        "SHOP_REWARDED_VIDEO",
        "SHOP_REWARDED_VIDEO_BALANCED",
        "CAPSTONE_COMPLETION",
        "WECHAT_STREAK_SHARING",
        "FRIENDS_QUEST",
        "FRIENDS_QUEST_GEMS",
        "FOR_YOU_TIERED_CHEST",
    }

    def _extract_gem_rewards(self, data, repeatable_only=False, tried_rids=None):
        if tried_rids is None:
            tried_rids = set()
        if repeatable_only:
            all_bundles = data.get("rewardBundles", [])
        else:
            all_bundles = data.get("rewardBundles", []) + data.get("currencyRewardBundles", [])
        gem_rids = []
        seen = set()
        for b in all_bundles:
            if b.get("empty", True) and not b.get("rewards"):
                continue
            b_type = b.get("rewardBundleType", "")
            if repeatable_only and b_type not in self.REPEATABLE_GEM_BUNDLES:
                continue
            for rew in b.get("rewards", []):
                if rew.get("consumed"):
                    continue
                rid = rew.get("id", "")
                if not rid or rid in seen:
                    continue
                # Chỉ lọc tried_rids đối với rương 1-lần (Round 1); rương lặp UUIDv3 chưa consumed thì luôn bắn tiếp
                if b_type not in self.REPEATABLE_GEM_BUNDLES and rid in tried_rids:
                    continue
                amt = rew.get("amount", 0)
                curr = rew.get("currency", "")
                items = rew.get("items", [])
                r_type = rew.get("rewardType", "")
                is_gem = (
                    curr == "GEMS"
                    or rid.endswith("-GEMS")
                    or rid.endswith("-GEM")
                    or r_type == "GEM_ADS"
                    or b_type in self.REPEATABLE_GEM_BUNDLES
                    or b_type in ("TIERED_TIMED_PATH_CHEST", "IMU_TIERED_PATH_CHEST", "WECHAT_SERVICE_ACCOUNT", "NOTIFICATION_OPT_IN_TIERED_CHEST", "FAMILY_QUEST_GEMS")
                    or any(it.get("itemType") in ("GEM", "GEMS") for it in items if isinstance(it, dict))
                )
                if is_gem:
                    if not amt and items:
                        for it in items:
                            if isinstance(it, dict) and it.get("amount"):
                                amt = it.get("amount", 0)
                    seen.add(rid)
                    gem_rids.append((amt or 25, rid))
        # Bắt buộc sắp xếp -1-GEMS trước -2-GEMS để không làm xoay UUID sớm trên CAPSTONE_COMPLETION / SKILL_COMPLETION_BALANCED
        gem_rids.sort(key=lambda x: (0 if "-1-" in x[1] else 1, -x[0], x[1]))
        return gem_rids

    def _patch_single_reward(self, rid, from_lang="en", to_lang="es", wait_on_429=False):
        web_headers = self._get_web_headers()
        consume_payload = {
            "fromLanguage": from_lang,
            "learningLanguage": to_lang,
            "timezone": "Asia/Ho_Chi_Minh"
        }
        patch_payload = {
            "consumed": True,
            "fromLanguage": from_lang,
            "learningLanguage": to_lang
        }
        for attempt in range(4):
            try:
                self._pace_request(0.04)
                r_c = self.session.put(
                    f"https://www.duolingo.com/rewards/users/{self.uid}/consume/{rid}",
                    headers=web_headers,
                    json=consume_payload,
                    timeout=8
                )
                if r_c.status_code == 200:
                    return "OK"
                if r_c.status_code == 429:
                    if not wait_on_429:
                        return "RATE_LIMIT"
                    return "RATE_LIMIT"
                # Fallback sang PATCH /rewards/{rid} nếu PUT trả về 400/404 cho một số loại rương cũ
                r_p = self.session.patch(
                    f"https://www.duolingo.com/2017-06-30/users/{self.uid}/rewards/{rid}?fields=id",
                    headers=web_headers,
                    json=patch_payload,
                    timeout=8
                )
                if r_p.status_code == 200:
                    return "OK"
                r_p2 = self.session.patch(
                    f"https://android-api-cf.duolingo.com/2017-06-30/users/{self.uid}/currency-rewards/{rid}?fields=id",
                    json=patch_payload,
                    timeout=8
                )
                if r_p2.status_code == 200:
                    return "OK"
                return "FAIL"
            except Exception:
                if attempt >= 2:
                    self.proxy_alive = False
                    self.session = self._create_session(use_proxy=False)
                else:
                    self.session = self._create_session()
        return "FAIL"

    def claim_gems_silent(self, rounds=4):
        try:
            web_headers = self._get_web_headers()
            base_r1 = f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig,fromLanguage,learningLanguage,rewardBundles,currencyRewardBundles"
            base_r2 = f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig,fromLanguage,learningLanguage,rewardBundles"
            initial_gems = None
            last_gems = 0
            tried_rids = set()
            for r_idx in range(1, max(1, rounds) + 1):
                u = f"{base_r1 if r_idx == 1 else base_r2}&_={int(time.time() * 1000)}"
                res = self.session.get(u, headers=web_headers, timeout=10)
                if res.status_code != 200:
                    break
                data = res.json()
                from_lang = data.get("fromLanguage", self.from_lang or "en")
                to_lang = data.get("learningLanguage", self.learning_lang or "es")
                cur_g = data.get("gems", data.get("gemsConfig", {}).get("gems", 0))
                if initial_gems is None:
                    initial_gems = cur_g
                last_gems = cur_g
                gem_rids = self._extract_gem_rewards(data, repeatable_only=(r_idx > 1), tried_rids=tried_rids)
                if not gem_rids:
                    break
                hit_rl = False
                for _, rid in gem_rids:
                    st = self._patch_single_reward(rid, from_lang, to_lang, wait_on_429=False)
                    if st == "OK":
                        tried_rids.add(rid)
                    elif st == "RATE_LIMIT":
                        hit_rl = True
                        break
                if hit_rl:
                    break
            res_end = self.session.get(f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig&_={int(time.time() * 1000)}", headers=web_headers, timeout=8)
            if res_end.status_code == 200:
                ed = res_end.json()
                last_gems = ed.get("gems", ed.get("gemsConfig", {}).get("gems", last_gems))
            return max(0, last_gems - (initial_gems or last_gems))
        except Exception:
            return 0

    def claim_gems(self, rounds=40, target_add_gems=None):
        print(cyan("[*] Đang kích hoạt Sequential UUIDv3 Queue Engine (Vét rương vô hạn + Tự động vượt Rate-Limit 30 rương/60s)..."))
        web_headers = self._get_web_headers()
        base_r1 = f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig,fromLanguage,learningLanguage,rewardBundles,currencyRewardBundles"
        base_r2 = f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig,fromLanguage,learningLanguage,rewardBundles"

        initial_gems = None
        current_gems = 0
        start_t = time.time()
        round_idx = 0
        tried_rids = set()
        zero_gain_streak = 0
        if target_add_gems is not None:
            max_rounds = max(100, (target_add_gems // 80) + 50)
        else:
            max_rounds = max(1, rounds)

        while round_idx < max_rounds:
            if self.stop_requested.is_set():
                print(gold("\n[!] Nhận được yêu cầu dừng, đang dừng tiến trình nhận Gems..."))
                break
            if target_add_gems is not None and initial_gems is not None:
                if (current_gems - initial_gems) >= target_add_gems:
                    break

            round_idx += 1
            fetch_url = f"{base_r1 if round_idx == 1 else base_r2}&_={int(time.time() * 1000)}"
            try:
                res = self.session.get(fetch_url, headers=web_headers, timeout=12)
                if res.status_code != 200:
                    time.sleep(0.3)
                    continue
                data = res.json()
            except Exception:
                self.proxy_alive = False
                self.session = self._create_session(use_proxy=False)
                continue

            from_lang = data.get("fromLanguage", self.from_lang or "en")
            to_lang = data.get("learningLanguage", self.learning_lang or "es")
            g_before = data.get("gems", data.get("gemsConfig", {}).get("gems", 0))
            if initial_gems is None:
                initial_gems = g_before
            current_gems = g_before

            if target_add_gems is not None and (current_gems - initial_gems) >= target_add_gems:
                break

            gem_rids = self._extract_gem_rewards(data, repeatable_only=(round_idx > 1), tried_rids=tried_rids)
            if not gem_rids:
                break

            # Bắn tuần tự theo đúng thứ tự (-1-GEMS trước -2-GEMS) và tự động đợi hồi Token Bucket nếu dính 429
            opened_this_round = 0
            for _, rid in gem_rids:
                if self.stop_requested.is_set():
                    break
                while True:
                    if self.stop_requested.is_set():
                        break
                    st = self._patch_single_reward(rid, from_lang, to_lang, wait_on_429=True)
                    if st == "OK":
                        tried_rids.add(rid)
                        opened_this_round += 1
                        break
                    elif st == "RATE_LIMIT":
                        # Nếu bị 429 thật từ server, chỉ đếm ngược thử 1 lần rồi kiểm tra lại; nếu vẫn RATE_LIMIT thì skip rid này để không bị kẹt vô tận
                        for wait_s in range(15, 0, -1):
                            if self.stop_requested.is_set():
                                break
                            gained_tmp = current_gems - (initial_gems or current_gems)
                            t_str = f"/{target_add_gems}" if target_add_gems else ""
                            sys.stdout.write(
                                f"\r\033[K\033[93m    [⏳] Đạt trần Server (+{gained_tmp}{t_str} Gems) -> Thử lại sau {wait_s}s...\033[0m"
                            )
                            sys.stdout.flush()
                            time.sleep(1)
                        if self.stop_requested.is_set():
                            break
                        # Thử lại đúng 1 lần cho rid này
                        st_retry = self._patch_single_reward(rid, from_lang, to_lang, wait_on_429=False)
                        if st_retry == "OK":
                            tried_rids.add(rid)
                            opened_this_round += 1
                        else:
                            tried_rids.add(rid)
                        break
                    else:
                        tried_rids.add(rid)
                        break

            if self.stop_requested.is_set():
                break

            # Kiểm tra số Gems mới ngay sau mỗi vòng (kèm cache-buster)
            try:
                r_chk = self.session.get(f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=gems,gemsConfig&_={int(time.time() * 1000)}", headers=web_headers, timeout=8)
                if r_chk.status_code == 200:
                    cd = r_chk.json()
                    current_gems = cd.get("gems", cd.get("gemsConfig", {}).get("gems", current_gems))
            except Exception:
                pass

            round_gain = current_gems - g_before
            if round_gain <= 0:
                zero_gain_streak += 1
            else:
                zero_gain_streak = 0

            gained_so_far = current_gems - (initial_gems or current_gems)
            elapsed = max(0.1, time.time() - start_t)
            rate = int(gained_so_far / elapsed)
            target_str = f"/{target_add_gems}" if target_add_gems else f" (Vòng {round_idx}/{max_rounds})"
            sys.stdout.write(
                f"\r\033[K\033[96m    ➔ Vòng {round_idx}: Đã mở {opened_this_round}/{len(gem_rids)} rương UUID (+{round_gain} vòng này) | Gems: {current_gems} (+{gained_so_far}{target_str}) | Tốc độ: ~{rate} Gems/s\033[0m"
            )
            sys.stdout.flush()

            if zero_gain_streak >= 3:
                break

        print()
        gained = max(0, current_gems - (initial_gems or current_gems))
        if gained > 0:
            print(green(f"[+] Thu hoạch thành công! +{gained} Gems | Tổng Gems: {current_gems}"))
        else:
            print(gold(f"[!] Đã vét cạn toàn bộ hàng đợi rương UUID trên tài khoản (Hiện có: {current_gems} Gems)."))
        return gained

    def trigger_referral_bot(self, num_clones=1):
        print(cyan(f"[*] Đang khởi động Referral Bot kích hoạt Super Duolingo & Quà tặng ({num_clones} lượt)..."))
        try:
            u_info = self.session.get(f"https://android-api-cf.duolingo.com/2017-06-30/users/{self.uid}?fields=inviteURL", timeout=10).json()
            invite_url = u_info.get("inviteURL", "")
            if not invite_url:
                print(gold("[!] Không tìm thấy Invite URL của tài khoản."))
                return False
            
            token = invite_url.rstrip("/").split("/")[-1]

            for i in range(num_clones):
                rand_str = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                clone_email = f"clone_{rand_str}@oatdopacmail.com"
                clone_pass = "TestPass123@"
                
                clone_sess = requests.Session(
                    impersonate="chrome124",
                    proxies={'http': self.proxy, 'https': self.proxy} if self.proxy else None
                )
                clone_sess.headers.update(self.headers)

                reg_payload = {
                    "age": 25,
                    "email": clone_email,
                    "name": f"Duo_{rand_str}",
                    "password": clone_pass,
                    "inviterId": self.uid,
                    "inviteToken": token,
                    "fromLanguage": "en",
                    "learningLanguage": "es",
                    "timezone": "Asia/Ho_Chi_Minh"
                }

                r_reg = clone_sess.post(
                    "https://android-api-cf.duolingo.com/2017-06-30/users?fields=id,username",
                    json=reg_payload,
                    timeout=12
                )
                if r_reg.status_code == 200:
                    c_jwt = r_reg.headers.get("jwt")
                    clone_sess.headers["Authorization"] = f"Bearer {c_jwt}"
                    
                    r_sess = clone_sess.post("https://android-api-cf.duolingo.com/2017-06-30/sessions", json={
                        "challengeTypes": ["translate"],
                        "fromLanguage": "en",
                        "isReview": False,
                        "learningLanguage": "es",
                        "type": "GLOBAL_PRACTICE"
                    }, timeout=12)
                    
                    if r_sess.status_code == 200:
                        s_data = r_sess.json()
                        sid = s_data.get("id")
                        solve_pl = {
                            k: v for k, v in s_data.items()
                            if k not in (
                                "challenges",
                                "adaptiveChallenges",
                                "adaptiveInterleavedChallenges",
                                "mistakesReplacementChallenges",
                                "sessionElementSolutions",
                                "ttsAnnotations",
                                "explanation"
                            )
                        }
                        solve_pl.update({
                            "challenges": [],
                            "adaptiveChallenges": [],
                            "sessionElementSolutions": [],
                            "heartsLeft": 5,
                            "startTime": int(time.time()) - 60,
                            "endTime": int(time.time()),
                            "enableBonusPoints": True,
                            "failed": False,
                            "hasBoost": True,
                            "maxInLessonStreak": 15,
                            "shouldLearnThings": True
                        })
                        r_sub = clone_sess.put(f"https://android-api-cf.duolingo.com/2017-06-30/sessions/{sid}", json=solve_pl, timeout=12)
                        if r_sub.status_code == 200:
                            print(green(f" [✓] Lượt {i+1}/{num_clones}: Đã kích hoạt thành công quà giới thiệu bạn bè & Super Duolingo!"))
            return True
        except Exception as e:
            print(red(f"[-] Lỗi kích hoạt referral: {e}"))
            return False

    def boost_gems(self, target_add_gems=4000):
        print(cyan("\n=================================================="))
        print(cyan("   QUY TRÌNH BUFF GEMS CHUYÊN SÂU (UUIDv3 QUEUE ENGINE) "))
        print(cyan("=================================================="))
        info = self.get_info()
        start_gems = info.get("gems", info.get("gemsConfig", {}).get("gems", 0)) if info else 0
        print(gold(f"[+] Gems ban đầu: {start_gems} | Mục tiêu bơm thêm: +{target_add_gems} Gems"))

        # Bước 1: Chạy Sequential UUIDv3 Queue Engine vét sạch các tầng rương lặp (32+ tiers ~ 4,000-4,600 Gems)
        print(cyan(f"\n[*] Bước 1/2: Quét & mở tuần tự toàn bộ hàng đợi rương UUIDv3 (Mục tiêu +{target_add_gems} Gems)..."))
        self.claim_gems(rounds=40, target_add_gems=target_add_gems)

        # Bước 2: Nếu chưa chạm mục tiêu, kích thêm Referral Bot và quét vét lần cuối
        final_info = self.get_info()
        end_gems = final_info.get("gems", final_info.get("gemsConfig", {}).get("gems", start_gems)) if final_info else start_gems
        gained = end_gems - start_gems
        if gained < target_add_gems:
            print(cyan("\n[*] Bước 2/2: Chạy Referral Bot kích hoạt thêm hòm quà bạn bè & quét vét..."))
            self.trigger_referral_bot(num_clones=1)
            self.claim_gems(rounds=5, target_add_gems=target_add_gems - gained)
            final_info = self.get_info()
            end_gems = final_info.get("gems", final_info.get("gemsConfig", {}).get("gems", end_gems)) if final_info else end_gems
            gained = end_gems - start_gems

        print(green(f"\n[✓] Hoàn tất Buff Gems! Gems: {start_gems} ➔ {end_gems} (+{gained} Gems)\n"))
        return gained

    def unlock_monthly_badges(self, mode="current", target_month=None, target_year=None):
        print(cyan("\n[*] Đang kết nối Goals API để tải Schema huy hiệu tháng..."))
        sess = self.get_worker_session()
        try:
            r_schema = sess.get("https://goals-api.duolingo.com/schema?ui_language=en", timeout=15)
            if r_schema.status_code != 200:
                print(red(f"[-] Không thể tải schema goals: {r_schema.status_code}"))
                return
            schema_data = r_schema.json()
            metrics = []
            for g in schema_data.get("goals", []):
                m = g.get("metric")
                if m and m not in metrics:
                    metrics.append(m)
        except Exception as e:
            print(gold(f"[!] Dùng danh sách metric dự phòng ({e})"))
            metrics = ["XP", "LESSONS", "QUESTS", "PERFECT_LESSONS", "STORIES"]

        metric_updates = [{"metric": m, "quantity": 1000} for m in metrics]
        if not any(mu["metric"] == "QUESTS" for mu in metric_updates):
            metric_updates.append({"metric": "QUESTS", "quantity": 50})

        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        targets = []

        if mode == "current":
            targets.append((now.year, now.month))
        elif mode == "specific" and target_month and target_year:
            targets.append((target_year, target_month))
        elif mode == "all":
            # Từ năm hiện tại lùi dần về 2021
            y = now.year
            m = now.month
            while y >= 2021:
                targets.append((y, m))
                m -= 1
                if m == 0:
                    m = 12
                    y -= 1

        print(cyan(f"[*] Bắt đầu mở khóa {len(targets)} Huy Hiệu Tháng (Monthly Badges)..."))
        success_count = 0

        for y, m in targets:
            dt = datetime.datetime(y, m, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            month_label = f"Tháng {m:02d}/{y}"
            
            payload = {
                "metric_updates": metric_updates,
                "timestamp": ts,
                "timezone": "Asia/Ho_Chi_Minh"
            }
            try:
                res = sess.post(f"https://goals-api.duolingo.com/users/{self.uid}/progress/batch", json=payload, timeout=15)
                if res.status_code == 200:
                    success_count += 1
                    print(green(f" [✓] Đã mở khóa thành công: Huy Hiệu {month_label}! (HTTP 200 SUCCESS)"))
                else:
                    print(gold(f" [!] {month_label}: Code {res.status_code} - {res.text[:60]}"))
            except Exception as e:
                print(red(f" [-] {month_label}: Lỗi kết nối ({e})"))

    def unlock_achievements(self, workers=20):
        print(cyan("\n=================================================="))
        print(cyan("   MỞ KHÓA THÀNH TỰU HỒ SƠ (ACHIEVEMENTS / AWARDS) "))
        print(cyan("=================================================="))
        sess = self.get_worker_session()
        h = self._get_web_headers()
        h_ios = dict(h)
        h_ios["User-Agent"] = "DuolingoMobile/7.45.0 (iPhone; iOS 18.0; Scale/3.00)"

        ACH_LABELS = [
            ("sharpshooter_expanded", "Flawless Finisher (500 bài hoàn hảo)"),
            ("legendary_level", "Legend (250 cấp Huyền Thoại)"),
            ("correct_mistakes", "Mistake Mechanic (1,000 lỗi sửa)"),
            ("challenger_expanded", "Speed Racer (7,500 XP Thử thách)"),
            ("kudos", "Cheerleader (100 Chúc mừng bạn bè)"),
            ("friendly", "Social Butterfly (Kết bạn)"),
            ("quest_champion", "Quest Explorer (Nhiệm vụ ngày)"),
            ("sage", "XP Olympian (30,000 XP)"),
            ("wildfire", "Wildfire (365 ngày Streak)"),
            ("nocturnal", "Sleepwalker (200 bài đêm)"),
            ("matinal", "Early Riser (200 bài sáng)"),
            ("regal_expanded", "Regal (Vương miện)"),
        ]

        def get_ach_map():
            try:
                r = sess.post(
                    f"https://duolingo-achievements-prod.duolingo.com/users/{self.uid}/migrate",
                    headers=h_ios, json={"async": False, "dryRun": False}, timeout=12
                )
                if r.status_code == 200:
                    return {a["name"]: a for a in r.json().get("achievements", [])}
            except Exception:
                pass
            return {}

        ach_map = get_ach_map()
        print(cyan("[*] Trạng thái Thành Tựu (Awards) hiện tại:"))
        for k, label in ACH_LABELS:
            a = ach_map.get(k) or ach_map.get(k.replace("_expanded", ""), {})
            tc = a.get("tierCounts", [])
            print(gold(f"    ➔ {label:<38}: count={a.get('count', 0)} | Tier {a.get('tier', 0)}/{len(tc)}"))

        # 1. Lấy skillId đầu tiên và courseId từ currentCourse
        first_skill_id = "057a0c44acfeb75183e4613ad7cecc66"
        course_id = f"DUOLINGO_{self.learning_lang.upper()}_{self.from_lang.upper()}"
        try:
            r_course = sess.get(
                f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse,currentCourseId",
                headers=h, timeout=12
            )
            if r_course.status_code == 200:
                cdata = r_course.json()
                if cdata.get("currentCourseId"):
                    course_id = cdata["currentCourseId"]
                cc = cdata.get("currentCourse", {}) or {}
                found = False
                for sec in cc.get("pathSectioned", []) or []:
                    for u in sec.get("units", []) or []:
                        for lvl in u.get("levels", []) or []:
                            sid = lvl.get("pathLevelClientData", {}).get("skillId")
                            if sid:
                                first_skill_id = sid
                                found = True
                                break
                        if found: break
                    if found: break
                if not found:
                    for row in cc.get("skills", []) or []:
                        for sk in row:
                            if sk.get("id"):
                                first_skill_id = sk["id"]
                                found = True
                                break
                        if found: break
        except Exception:
            pass

        now_ts = int(time.time())

        # 2. Bơm Flawless Finisher (sharpshooter & sharpshooter_expanded -> Max 500 bài không lỗi)
        sharp_count = max(
            ach_map.get("sharpshooter_expanded", {}).get("count", 0),
            ach_map.get("sharpshooter", {}).get("count", 0)
        )
        if sharp_count < 500:
            needed_sharp = (500 - sharp_count) + 5
            print(cyan(f"\n[*] Đang bơm 'Flawless Finisher' (+{needed_sharp} bài học hoàn hảo lên 500/500)..."))
            try:
                r_post = sess.post(
                    "https://www.duolingo.com/2017-06-30/sessions",
                    headers=h,
                    json={
                        "challengeTypes": ["translate"],
                        "fromLanguage": self.from_lang,
                        "isFinalLevel": False,
                        "isV2": True,
                        "juicy": True,
                        "learningLanguage": self.learning_lang,
                        "smartTipsVersion": 2,
                        "type": "LESSON",
                        "skillId": first_skill_id,
                        "levelIndex": 0,
                        "lessonIndex": 0,
                    },
                    timeout=12
                )
                if r_post.status_code == 200:
                    tpl_lesson = r_post.json()
                    base_sid = tpl_lesson.get("id", "")[:10]

                    def put_perfect_lesson(idx):
                        ws = self.get_worker_session()
                        fake_sid = base_sid + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
                        p = dict(tpl_lesson)
                        p["id"] = fake_sid
                        p["challenges"] = []
                        p["AdaptiveChallenges"] = []
                        p["adaptiveInterleavedChallenges"] = {}
                        p["failed"] = False
                        p["maxInLessonStreak"] = 15
                        p["numHearts"] = 5
                        p["startTime"] = now_ts - 120
                        p["endTime"] = now_ts
                        try:
                            res = ws.put(f"https://www.duolingo.com/2017-06-30/sessions/{fake_sid}", headers=h, json=p, timeout=10)
                            return res.status_code == 200
                        except Exception:
                            return False

                    with ThreadPoolExecutor(max_workers=min(workers, 25)) as ex:
                        res_list = list(ex.map(put_perfect_lesson, range(needed_sharp)))
                    print(green(f" [✓] Đã bơm {sum(1 for x in res_list if x)}/{needed_sharp} bài Flawless Finisher!"))
            except Exception as e:
                print(gold(f" [!] Lỗi khi bơm Flawless Finisher: {e}"))

        # 3. Bơm Legend (legendary_level -> Max 250 bài Huyền Thoại)
        leg_count = ach_map.get("legendary_level", {}).get("count", 0)
        if leg_count < 250:
            needed_leg = (250 - leg_count) + 5
            print(cyan(f"\n[*] Đang bơm 'Legend' (+{needed_leg} cấp Huyền Thoại lên 250/250)..."))
            try:
                r_post_leg = sess.post(
                    "https://android-api-cf.duolingo.com/2017-06-30/sessions",
                    headers=h,
                    json={
                        "challengeTypes": ["translate"],
                        "fromLanguage": self.from_lang,
                        "isFinalLevel": False,
                        "isV2": True,
                        "juicy": True,
                        "learningLanguage": self.learning_lang,
                        "smartTipsVersion": 2,
                        "type": "LEGENDARY_LEVEL",
                        "skillId": first_skill_id,
                        "levelIndex": 0,
                    },
                    timeout=12
                )
                if r_post_leg.status_code == 200:
                    tpl_leg = r_post_leg.json()
                    base_leg_sid = tpl_leg.get("id", "")[:10]

                    def put_legendary(idx):
                        ws = self.get_worker_session()
                        fake_sid = base_leg_sid + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
                        p = dict(tpl_leg)
                        p["id"] = fake_sid
                        p["challenges"] = []
                        p["AdaptiveChallenges"] = []
                        p["adaptiveInterleavedChallenges"] = {}
                        p["failed"] = False
                        p["maxInLessonStreak"] = 15
                        p["startTime"] = now_ts - 120
                        p["endTime"] = now_ts
                        try:
                            res = ws.put(f"https://android-api-cf.duolingo.com/2017-06-30/sessions/{fake_sid}", headers=h, json=p, timeout=10)
                            return res.status_code == 200
                        except Exception:
                            return False

                    with ThreadPoolExecutor(max_workers=min(workers, 25)) as ex:
                        res_list = list(ex.map(put_legendary, range(needed_leg)))
                    print(green(f" [✓] Đã bơm {sum(1 for x in res_list if x)}/{needed_leg} bài Legend!"))
            except Exception as e:
                print(gold(f" [!] Lỗi khi bơm Legend: {e}"))

        # 4. Bơm Mistake Mechanic (correct_mistakes -> Max 1,000 lỗi sửa qua iOS MISTAKES_REVIEW x80)
        cm_count = ach_map.get("correct_mistakes", {}).get("count", 0)
        if cm_count < 1000:
            print(cyan(f"\n[*] Đang bơm 'Mistake Mechanic' ({cm_count} ➔ 1,000/1,000 lỗi sửa qua iOS MISTAKES_REVIEW)..."))
            try:
                r_m = sess.get(f"https://ios-api-2.duolingo.com/v2/mistakes/users/{self.uid}/courses/{course_id}?limit=20", headers=h_ios, timeout=10)
                mistakes = r_m.json().get("mistakes", []) if r_m.status_code == 200 else []
                if not mistakes:
                    # Gieo 3 lỗi mẫu từ 1 bài LESSON để kích hoạt danh sách Mistakes
                    r_seed = sess.post(
                        "https://ios-api-2.duolingo.com/2017-06-30/sessions",
                        headers=h_ios,
                        json={
                            "challengeTypes": ["translate", "listen"],
                            "fromLanguage": self.from_lang,
                            "learningLanguage": self.learning_lang,
                            "type": "LESSON",
                            "skillId": first_skill_id,
                            "levelIndex": 0,
                            "lessonIndex": 0,
                            "isV2": True,
                            "juicy": True,
                        },
                        timeout=12
                    )
                    if r_seed.status_code == 200:
                        s_seed = r_seed.json()
                        for ch in s_seed.get("challenges", [])[:3]:
                            ch["correct"] = False
                            ch["failed"] = True
                        s_seed["failed"] = False
                        s_seed["startTime"] = now_ts - 60
                        s_seed["endTime"] = now_ts
                        sess.put(f"https://ios-api-2.duolingo.com/2017-06-30/sessions/{s_seed['id']}", headers=h_ios, json=s_seed, timeout=10)
                        r_m = sess.get(f"https://ios-api-2.duolingo.com/v2/mistakes/users/{self.uid}/courses/{course_id}?limit=20", headers=h_ios, timeout=10)
                        mistakes = r_m.json().get("mistakes", []) if r_m.status_code == 200 else []

                real_cgis = [m.get("challengeGeneratorIdentifier") for m in mistakes if m.get("challengeGeneratorIdentifier")]
                if real_cgis:
                    r_mr = sess.post(
                        "https://ios-api-2.duolingo.com/2017-06-30/sessions",
                        headers=h_ios,
                        json={
                            "challengeTypes": ["translate", "listen", "name", "assist", "fill", "gapFill"],
                            "fromLanguage": self.from_lang,
                            "learningLanguage": self.learning_lang,
                            "type": "MISTAKES_REVIEW",
                            "isV2": True,
                            "juicy": True,
                            "smartTipsVersion": 2,
                            "generatorIdentifiersOfRecentMistakes": real_cgis,
                        },
                        timeout=12
                    )
                    if r_mr.status_code == 200:
                        tpl_mr = r_mr.json()
                        base_mr_sid = tpl_mr.get("id", "")[:10]
                        slim_chs = []
                        for ch in tpl_mr.get("challenges", []):
                            slim_chs.append({
                                "id": ch.get("id"),
                                "type": ch.get("type"),
                                "correct": True,
                                "challengeGeneratorIdentifier": ch.get("challengeGeneratorIdentifier"),
                                "indicatorType": ch.get("indicatorType", "PAST_MISTAKE"),
                                "metadata": {
                                    "indicator_type": {"name": "PAST_MISTAKE"},
                                    "specific_type": "tap",
                                    "type": ch.get("type", "translate"),
                                }
                            })
                        per_put = max(len(slim_chs) * 80, 1)
                        needed_puts = ((1000 - cm_count) // per_put) + 2
                        ok_mr = 0
                        for _ in range(needed_puts):
                            fake_sid = base_mr_sid + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
                            p_mr = {
                                "id": fake_sid,
                                "type": "MISTAKES_REVIEW",
                                "fromLanguage": self.from_lang,
                                "learningLanguage": self.learning_lang,
                                "courseId": course_id,
                                "failed": False,
                                "challenges": slim_chs * 80,
                                "startTime": int(time.time()) - 60,
                                "endTime": int(time.time()),
                            }
                            rc_mr = sess.put(f"https://ios-api-2.duolingo.com/2017-06-30/sessions/{fake_sid}", headers=h_ios, json=p_mr, timeout=15)
                            if rc_mr.status_code == 200:
                                ok_mr += 1
                        print(green(f" [✓] Đã bơm {ok_mr}/{needed_puts} gói Mistake Mechanic (+{ok_mr * per_put} lỗi sửa)!"))
            except Exception as e:
                print(gold(f" [!] Lỗi khi bơm Mistake Mechanic: {e}"))

        # 5. Bơm Speed Racer (challenger & challenger_expanded -> Max 7,500 XP Thử thách tính giờ)
        chal_count = max(
            ach_map.get("challenger_expanded", {}).get("count", 0),
            ach_map.get("challenger", {}).get("count", 0)
        )
        if chal_count < 7500:
            needed_chal_puts = ((7500 - chal_count) // 80) + 4
            print(cyan(f"\n[*] Đang bơm 'Speed Racer' ({chal_count} ➔ 7,500/7,500 XP qua {needed_chal_puts} gói iOS MATCH_PRACTICE)..."))
            try:
                r_mp = sess.post(
                    "https://ios-api-2.duolingo.com/2017-06-30/sessions",
                    headers=h_ios,
                    json={
                        "challengeTypes": ["match"],
                        "fromLanguage": self.from_lang,
                        "learningLanguage": self.learning_lang,
                        "type": "MATCH_PRACTICE",
                        "skillId": first_skill_id,
                        "levelIndex": 0,
                        "stepIndex": 0,
                        "isV2": True,
                        "juicy": True,
                    },
                    timeout=12
                )
                if r_mp.status_code == 200:
                    base_mp_sid = r_mp.json().get("id", "")[:10]

                    def put_match_practice(idx):
                        ws = self.get_worker_session()
                        fake_sid = base_mp_sid + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
                        p_mp = {
                            "id": fake_sid,
                            "type": "MATCH_PRACTICE",
                            "fromLanguage": self.from_lang,
                            "learningLanguage": self.learning_lang,
                            "courseId": course_id,
                            "failed": False,
                            "challenges": [],
                            "heartsLeft": 5,
                            "xpGain": 500,
                            "xpPromised": 500,
                            "hasBoost": True,
                            "completedSegments": 3,
                            "startTime": int(time.time()) - 60,
                            "endTime": int(time.time()),
                        }
                        try:
                            res = ws.put(f"https://ios-api-2.duolingo.com/2017-06-30/sessions/{fake_sid}", headers=h_ios, json=p_mp, timeout=10)
                            return res.status_code == 200
                        except Exception:
                            return False

                    with ThreadPoolExecutor(max_workers=min(workers, 25)) as ex:
                        res_list = list(ex.map(put_match_practice, range(needed_chal_puts)))
                    print(green(f" [✓] Đã bơm {sum(1 for x in res_list if x)}/{needed_chal_puts} gói Speed Racer!"))
            except Exception as e:
                print(gold(f" [!] Lỗi khi bơm Speed Racer: {e}"))

        # 6. Bơm Social Butterfly (friendly -> Kết bạn >= 3 người) & Cheerleader (kudos -> 100 chúc mừng)
        try:
            sess.post(
                f"https://www.duolingo.com/2017-06-30/friends/users/{self.uid}/request-parental-consent-for-social",
                headers=h, json={"action": "enableSocial"}, timeout=8
            )
            if ach_map.get("friendly", {}).get("count", 0) < 3:
                print(cyan("\n[*] Đang mở khóa 'Social Butterfly' (Follow 5 người dùng gợi ý)..."))
                r_rec = sess.get(f"https://www.duolingo.com/2017-06-30/friends/users/{self.uid}/recommendations?pageSize=10", headers=h, timeout=8)
                if r_rec.status_code == 200:
                    for rec in r_rec.json().get("recommendations", [])[:5]:
                        ruid = rec.get("userId")
                        if ruid:
                            sess.post(f"https://www.duolingo.com/2017-06-30/friends/users/{self.uid}/follow/{ruid}", headers=h, json={"component": "profile", "follow_reason": "recommendations"}, timeout=8)

            kudos_count = ach_map.get("kudos", {}).get("count", 0)
            if kudos_count < 100:
                needed_kudos = 100 - kudos_count
                num_helpers = max(1, (needed_kudos // 20) + 1)
                print(cyan(f"\n[*] Đang bơm 'Cheerleader' ({kudos_count} ➔ 100/100 qua {num_helpers} Helper Bot xác thực Email)..."))
                import re

                def spawn_verified_helper(idx):
                    try:
                        s_c = requests.Session(impersonate="chrome124")
                        tm = s_c.post("https://api.internal.temp-mail.io/api/v3/email/new", json={"min_name_length": 10, "max_name_length": 10}, timeout=8).json()
                        addr = tm.get("email")
                        if not addr:
                            return None
                        r_reg = s_c.post(
                            "https://android-api-cf.duolingo.com/2017-06-30/users?fields=id,username,emailVerified",
                            headers={"User-Agent": "Duodroid/5.141.11 Dalvik/2.1.0 (Linux; U; Android 9; SM-S9280 Build/PQ3A.190705.08272029)", "Content-Type": "application/json"},
                            json={"age": 25, "email": addr, "name": "DuoHelper", "password": "TestPass123@", "fromLanguage": "en", "learningLanguage": "es", "timezone": "Asia/Ho_Chi_Minh"},
                            timeout=12
                        )
                        c_uid = r_reg.json().get("id")
                        c_jwt = r_reg.headers.get("jwt")
                        if not c_uid or not c_jwt:
                            return None
                        hc = {
                            "Authorization": f"Bearer {c_jwt}",
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "Content-Type": "application/json"
                        }
                        for _ in range(8):
                            time.sleep(1.5)
                            msgs = s_c.get(f"https://api.internal.temp-mail.io/api/v3/email/{addr}/messages", timeout=8).json()
                            if msgs:
                                body = str(msgs[0].get("body_html") or msgs[0].get("body_text") or msgs[0])
                                matches = re.findall(r"https://www\.duolingo\.com/verify/([A-Za-z0-9_]+)", body)
                                if matches:
                                    v_tok = matches[-1]
                                    rv = s_c.patch(f"https://www.duolingo.com/2017-06-30/users/{c_uid}/email-verifications/{v_tok}", headers=hc, json={"verified": True}, timeout=8)
                                    s_c.post(f"https://www.duolingo.com/2017-06-30/friends/users/{c_uid}/request-parental-consent-for-social", headers=hc, json={"action": "enableSocial"}, timeout=8)
                                    if rv.status_code == 200 and "true" in rv.text.lower():
                                        return s_c, c_uid, hc
                                    break
                    except Exception:
                        pass
                    return None

                with ThreadPoolExecutor(max_workers=min(num_helpers, 5)) as ex:
                    helpers = [hp for hp in ex.map(spawn_verified_helper, range(num_helpers)) if hp]

                seen_eids = set()
                new_reacts = 0
                for s_c, c_uid, hc in helpers:
                    for _ in range(25):
                        rf = s_c.post(f"https://www.duolingo.com/2017-06-30/friends/users/{c_uid}/follow/{self.uid}", headers=hc, json={"component": "profile", "follow_reason": "search"}, timeout=8)
                        if rf.status_code == 429:
                            break
                        r_feed = sess.get(f"https://www.duolingo.com/2017-06-30/friends/users/{self.uid}/feed/v2?uiLanguage=en", headers=h, timeout=8)
                        if r_feed.status_code == 200:
                            for f_item in r_feed.json().get("feed", []):
                                for c in f_item.get("feedCards", []):
                                    eid = c.get("eventId")
                                    if eid and eid not in seen_eids:
                                        seen_eids.add(eid)
                                        rr = sess.post("https://www.duolingo.com/card/reaction", headers=h, json={
                                            "groupId": eid, "reaction": "CONGRATS", "reactionTimestamp": int(time.time() * 1000), "userId": self.uid
                                        }, timeout=8)
                                        if rr.status_code == 200:
                                            new_reacts += 1
                        s_c.delete(f"https://www.duolingo.com/2017-06-30/friends/users/{c_uid}/follow/{self.uid}", headers=hc, timeout=8)
                        if new_reacts >= needed_kudos + 3:
                            break
                    if new_reacts >= needed_kudos + 3:
                        break
                print(green(f" [✓] Đã gửi +{new_reacts} lượt chúc mừng bạn bè (Cheerleader)!"))
        except Exception as e:
            print(gold(f" [!] Lỗi khi bơm Cheerleader / Social Butterfly: {e}"))

        # 7. Bơm Quest Explorer (quest_champion) qua Goals API + Achievements Quest Endpoint
        print(cyan("\n[*] Đang kích hoạt chuỗi nhiệm vụ 'Quest Explorer' (Daily / Friends Quests)..."))
        for qt in ["DAILY", "CORE", "HARD", "FRIENDS", "FAMILY"]:
            try:
                sess.post(
                    f"https://duolingo-achievements-prod.duolingo.com/users/{self.uid}/quest",
                    headers=h_ios, json={"questType": qt}, timeout=8
                )
            except Exception:
                pass
        try:
            r_sc = sess.get("https://goals-api.duolingo.com/schema?ui_language=en", headers=h, timeout=10)
            if r_sc.status_code == 200:
                all_metrics = list({g.get("metric") for g in r_sc.json().get("goals", []) if g.get("metric")})
                m_updates = [{"metric": m, "quantity": 2000} for m in all_metrics]
                import datetime
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                sess.post(
                    f"https://goals-api.duolingo.com/users/{self.uid}/progress/batch",
                    headers=h,
                    json={"metric_updates": m_updates, "timestamp": now_utc.strftime("%Y-%m-%dT12:00:00.000Z"), "timezone": "Asia/Ho_Chi_Minh"},
                    timeout=10
                )
        except Exception:
            pass

        # 8. Đồng bộ & Claim toàn bộ các Tier đã mở khóa để hiện vàng rực trên App
        ach_map = get_ach_map()
        claimed_total = 0
        print(cyan("\n[*] Đang Claim toàn bộ Huy Chương Thành Tựu đã đạt mốc..."))
        for name, a in ach_map.items():
            tier = a.get("tier", 0)
            for t in range(1, tier + 1):
                try:
                    rc = sess.post(
                        f"https://duolingo-achievements-prod.duolingo.com/users/{self.uid}/{name}/{t}/claim",
                        headers=h_ios, json={}, timeout=8
                    )
                    if rc.status_code == 200:
                        claimed_total += 1
                except Exception:
                    pass

        print(green(f"\n[✓] Hoàn tất Mở Khóa & Claim Thành Tựu ({claimed_total} mốc huy chương):"))
        for k, label in ACH_LABELS:
            a = ach_map.get(k) or ach_map.get(k.replace("_expanded", ""), {})
            tc = a.get("tierCounts", [])
            print(green(f"    [✓] {label:<38}: count={a.get('count', 0)} | Tier {a.get('tier', 0)}/{len(tc)}"))

    def _solve_single_path_level(self, lvl, course_id, tree_id, f_lang, l_lang, rbundles, web_headers):
        ltype = lvl.get("type")
        subtype = lvl.get("subtype")
        cdata = lvl.get("pathLevelClientData", {}) or {}
        meta = lvl.get("pathLevelMetadata", {}) or {}
        smeta = lvl.get("pathLevelSessionMetadata", {}) or {}
        now = int(time.time())

        if ltype == "chest":
            for btype in ["PATH_CHEST", "IMU_TIERED_PATH_CHEST", "TIERED_PATH_CHEST", "TIMED_PATH_CHEST", "TIERED_TIMED_PATH_CHEST", "SKILL_COMPLETION"]:
                b_obj = next((rb for rb in rbundles if rb.get("rewardBundleType") == btype), None)
                if not b_obj:
                    continue
                for rew in b_obj.get("rewards", []):
                    if rew.get("consumed"):
                        continue
                    rid = rew.get("id")
                    rc = self.session.put(
                        f"https://www.duolingo.com/rewards/users/{self.uid}/consume/{rid}",
                        headers=web_headers,
                        json={
                            "fromLanguage": f_lang,
                            "learningLanguage": l_lang,
                            "pathLevelSpecifics": meta,
                            "timezone": "Asia/Ho_Chi_Minh"
                        },
                        timeout=12
                    )
                    if rc.status_code == 200:
                        return True
            return False

        if ltype == "story":
            story_id = cdata.get("storyId") or meta.get("storyId")
            if not story_id:
                return False
            spl = {
                "awardXp": True,
                "completedBonusChallenge": True,
                "courseId": course_id,
                "dailyRefreshInfo": lvl.get("dailyRefreshInfo"),
                "fromLanguage": f_lang,
                "learningLanguage": l_lang,
                "hasXpBoost": False,
                "illustrationFormat": "svg",
                "isFeaturedStoryInPracticeHub": False,
                "isLegendaryMode": False,
                "isV2Redo": False,
                "isV2Story": True,
                "levelId": lvl.get("id"),
                "levelSessionIndex": lvl.get("finishedSessions", 0),
                "masterVersion": True,
                "maxScore": 5,
                "mode": (cdata.get("mode") or meta.get("mode") or "READ").upper(),
                "pathLevelSpecifics": meta,
                "score": 5,
                "startTime": now - 60,
                "endTime": now,
                "treeId": tree_id,
            }
            rs = self.session.post(
                f"https://stories.duolingo.com/api2/stories/{story_id}/complete",
                headers=web_headers,
                json=spl,
                timeout=12
            )
            return rs.status_code == 200

        total_sess = max(1, int(lvl.get("totalSessions", 1) or 1))
        done_sess = int(lvl.get("finishedSessions", 0) or 0)
        rem_sess = max(1, total_sess - done_sess)
        has_lvl_rev = bool(lvl.get("hasLevelReview", False))

        for s_offset in range(rem_sess):
            cur_sess_idx = done_sess + s_offset
            is_review_sess = has_lvl_rev and (cur_sess_idx == total_sess - 1)
            now = int(time.time())
            pl = {
                "challengeTypes": ["translate", "assist", "listen"],
                "fromLanguage": f_lang,
                "learningLanguage": l_lang,
                "isFinalLevel": False,
                "isRedoingPassedNode": False,
                "isV2": True,
                "juicy": True,
                "smartTipsVersion": 2,
                "treeId": tree_id,
                "levelId": lvl.get("id"),
                "levelSessionIndex": cur_sess_idx,
                "pathLevelSessionMetadata": smeta,
            }
            if ltype == "skill":
                pl["type"] = "LEVEL_REVIEW" if is_review_sess else "LESSON"
                pl["skillId"] = cdata.get("skillId") or meta.get("skillId")
                pl["levelIndex"] = cdata.get("crownLevelIndex", 0)
                if not is_review_sess:
                    pl["isCustomIntroSkill"] = (subtype == "custom_intro")
                    pl["isGrammarSkill"] = (subtype == "grammar")
                    pl["showGrammarSkillSplash"] = False
            elif ltype == "duo_radio":
                summary = cdata.get("duoRadioSummary") or meta.get("duoRadioSummary") or {}
                pl["type"] = "DUORADIO"
                pl["duoRadioSessionId"] = summary.get("id")
                pl["episodeId"] = summary.get("episode_id")
            elif ltype == "practice":
                ptype = cdata.get("practiceType")
                sr_ids = cdata.get("spacedRepetitionSkillIds", [])
                s_ids = cdata.get("skillIds", [])
                if subtype == "unit_practice":
                    pl["type"] = "UNIT_PRACTICE"
                    pl["skillIds"] = s_ids
                elif ptype == "SPACED_REPETITION" and sr_ids:
                    pl["type"] = "SPACED_REPETITION"
                    pl["skillIds"] = sr_ids
                else:
                    pl["type"] = "LEXEME_PRACTICE"
                    pl["skillIds"] = s_ids
                pl["lexemePracticeType"] = "practice_level_review" if is_review_sess else "practice_level"
            elif ltype == "unit_review":
                pl["type"] = "UNIT_REVIEW"
                pl["skillIds"] = cdata.get("skillIds", [])
            elif ltype == "alphabet":
                pl["type"] = "ALPHABET_LESSON"
                pl["alphabetId"] = cdata.get("alphabetId")
                pl["alphabetSessionId"] = cdata.get("alphabetSessionId")
                pl["showAlphabetTipSplash"] = False
            else:
                return False

            rs = self.session.post(
                "https://www.duolingo.com/2017-06-30/sessions",
                headers=web_headers,
                json=pl,
                timeout=12
            )
            if rs.status_code != 200 and ltype == "skill":
                # Fallback giữa LEVEL_REVIEW và LESSON nếu cấu hình node khác dự kiến
                pl["type"] = "LESSON" if pl["type"] == "LEVEL_REVIEW" else "LEVEL_REVIEW"
                pl["isCustomIntroSkill"] = (subtype == "custom_intro")
                pl["isGrammarSkill"] = (subtype == "grammar")
                pl["showGrammarSkillSplash"] = False
                rs = self.session.post(
                    "https://www.duolingo.com/2017-06-30/sessions",
                    headers=web_headers,
                    json=pl,
                    timeout=12
                )
            if rs.status_code != 200:
                return False
            sdata = rs.json()
            sid = sdata.get("id")
            if not sid:
                return False
            for k in ["adaptiveChallenges", "adaptiveInterleavedChallenges", "mistakesReplacementChallenges", "elements"]:
                sdata.pop(k, None)
            sdata["challenges"] = []
            sdata["courseId"] = course_id
            sdata["fromLanguage"] = f_lang
            sdata["learningLanguage"] = l_lang
            sdata["type"] = pl["type"]
            sdata["dailyRefreshInfo"] = lvl.get("dailyRefreshInfo")
            sdata["pathLevelSpecifics"] = meta
            sdata["failed"] = False
            sdata["heartsLeft"] = 0
            sdata["startTime"] = now - 60
            sdata["endTime"] = now
            sdata["enableBonusPoints"] = False
            sdata["maxInLessonStreak"] = 15
            sdata["shouldLearnThings"] = True
            if ltype == "duo_radio":
                sdata["duoRadioChallengeStats"] = {
                    "numChallengesCorrect": 5,
                    "numChallengesCompleted": 5
                }
            rput = self.session.put(
                f"https://www.duolingo.com/2017-06-30/sessions/{sid}",
                headers=web_headers,
                json=sdata,
                timeout=12
            )
            if rput.status_code != 200 and ltype == "skill":
                # Fallback tạo lại session với LESSON nếu PUT LEVEL_REVIEW trả 500
                alt_type = "LESSON" if pl["type"] == "LEVEL_REVIEW" else "LEVEL_REVIEW"
                pl["type"] = alt_type
                pl["isCustomIntroSkill"] = (subtype == "custom_intro")
                pl["isGrammarSkill"] = (subtype == "grammar")
                pl["showGrammarSkillSplash"] = False
                rs2 = self.session.post(
                    "https://www.duolingo.com/2017-06-30/sessions",
                    headers=web_headers,
                    json=pl,
                    timeout=12
                )
                if rs2.status_code == 200:
                    sdata2 = rs2.json()
                    sid2 = sdata2.get("id")
                    for k in ["adaptiveChallenges", "adaptiveInterleavedChallenges", "mistakesReplacementChallenges", "elements"]:
                        sdata2.pop(k, None)
                    sdata2["challenges"] = []
                    sdata2["courseId"] = course_id
                    sdata2["fromLanguage"] = f_lang
                    sdata2["learningLanguage"] = l_lang
                    sdata2["type"] = alt_type
                    sdata2["dailyRefreshInfo"] = lvl.get("dailyRefreshInfo")
                    sdata2["pathLevelSpecifics"] = meta
                    sdata2["failed"] = False
                    sdata2["heartsLeft"] = 0
                    sdata2["startTime"] = now - 60
                    sdata2["endTime"] = now
                    sdata2["enableBonusPoints"] = False
                    sdata2["maxInLessonStreak"] = 15
                    sdata2["shouldLearnThings"] = True
                    rput = self.session.put(
                        f"https://www.duolingo.com/2017-06-30/sessions/{sid2}",
                        headers=web_headers,
                        json=sdata2,
                        timeout=12
                    )
            if rput.status_code != 200:
                return False
        return True

    def _upgrade_course_nodes_to_legendary(self, course_id, tree_id, f_lang, l_lang, c_title, web_headers, max_units=None):
        print(gold(f"\n    [★] Bắt đầu CÀO FULL LEGENDARY SIÊU TỐC (1-PUT Ultra-Slim fake_sid) cho khóa {c_title}..."))
        chunk_size = 40
        total_upgraded = 0

        try:
            r_init = self.session.get(
                f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse{{id,treeId,pathSectioned{{index,type,units{{unitIndex,levels{{id,state,type,subtype,pathLevelClientData,pathLevelMetadata,pathLevelSessionMetadata,dailyRefreshInfo}}}}}}}}&_={int(time.time()*1000)}",
                headers=web_headers,
                timeout=18
            )
            if r_init.status_code != 200:
                return 0
            cc_init = r_init.json().get("currentCourse", {}) or {}
            if not tree_id:
                tree_id = cc_init.get("treeId")
            secs_init = cc_init.get("pathSectioned", []) or []
        except Exception:
            return 0

        all_units = []
        node_map = {}
        first_skill_lvl = None
        for s in secs_init:
            if s.get("type") == "learning":
                for u in s.get("units", []):
                    u_idx = u.get("unitIndex")
                    all_units.append((s.get("index"), u_idx))
                    for l_idx, l in enumerate(u.get("levels", [])):
                        if l.get("type") in ("skill", "practice", "story"):
                            node_map[(u_idx, l_idx)] = l
                            if first_skill_lvl is None and l.get("type") == "skill":
                                first_skill_lvl = l

        if max_units and max_units > 0:
            allowed_u = set(x[1] for x in all_units[:max_units])
        else:
            allowed_u = None

        pending_unit_indices = set()
        for s in secs_init:
            if s.get("type") == "learning":
                for u in s.get("units", []):
                    u_idx = u.get("unitIndex")
                    if allowed_u is not None and u_idx not in allowed_u and len(pending_unit_indices) >= max_units:
                        continue
                    if any(l.get("state") == "passed" and l.get("type") in ("skill", "practice", "story") for l in u.get("levels", [])):
                        pending_unit_indices.add(u_idx)
                        if max_units and len(pending_unit_indices) >= max_units:
                            break

        target_unit_list = sorted(list(pending_unit_indices))
        if not target_unit_list:
            print(green(f"    [✓] Toàn bộ các Unit mục tiêu của khóa {c_title} đã đạt 100% Vàng Huyền Thoại (Legendary)!"))
            return 0

        # Khởi tạo 1-PUT Session Template duy nhất cho toàn khóa học (Cắt bỏ 100% lệnh POST /sessions nặng 600KB ở từng node)
        leg_tmpl = None
        base_sid = "6347SDuHAe"
        if first_skill_lvl is not None:
            cdata0 = first_skill_lvl.get("pathLevelClientData", {}) or {}
            meta0 = first_skill_lvl.get("pathLevelMetadata", {}) or {}
            smeta0 = first_skill_lvl.get("pathLevelSessionMetadata", {}) or {}
            pl0 = {
                "challengeTypes": ["translate", "assist", "listen"],
                "fromLanguage": f_lang,
                "learningLanguage": l_lang,
                "isFinalLevel": True,
                "isRedoingPassedNode": True,
                "isV2": True,
                "juicy": True,
                "smartTipsVersion": 2,
                "treeId": tree_id,
                "levelId": first_skill_lvl.get("id"),
                "levelSessionIndex": 0,
                "pathLevelSessionMetadata": smeta0,
                "type": "LEGENDARY_LEVEL",
                "skillId": cdata0.get("skillId") or meta0.get("skillId"),
                "levelIndex": cdata0.get("crownLevelIndex", 0),
            }
            for _ in range(3):
                try:
                    rs0 = self.session.post("https://www.duolingo.com/2017-06-30/sessions", headers=web_headers, json=pl0, timeout=12)
                    if rs0.status_code == 200:
                        leg_tmpl = rs0.json()
                        for k in ["adaptiveChallenges", "adaptiveInterleavedChallenges", "mistakesReplacementChallenges", "elements", "ttsAnnotations"]:
                            leg_tmpl.pop(k, None)
                        leg_tmpl["challenges"] = []
                        if leg_tmpl.get("id") and len(leg_tmpl["id"]) >= 10:
                            base_sid = leg_tmpl["id"][:10]
                        break
                except Exception:
                    time.sleep(0.3)

        cf_hosts = [
            "https://www.duolingo.com",
            "https://excess.duolingo.com",
            "https://android-api-cf.duolingo.com",
        ]
        for h_url in cf_hosts:
            try:
                self.session.get(f"{h_url}/2017-06-30/users/{self.uid}?fields=id", headers=web_headers, timeout=5)
            except Exception:
                pass

        host_counter = [0]
        pace_lock = threading.Lock()
        last_ts = [0.0]

        def pace_gateway(interval=0.52):
            with pace_lock:
                now = time.time()
                w = last_ts[0] + interval - now
                if w > 0:
                    time.sleep(w)
                last_ts[0] = time.time()
                h_url = cf_hosts[host_counter[0] % len(cf_hosts)]
                host_counter[0] += 1
                return h_url

        print(cyan(f"    [*] Phát hiện {len(target_unit_list)} Units cần mạ vàng Legendary. Đang chạy 1-PUT Pipelined Multi-Gateway..."))
        t_course_start = time.time()

        for c_start in range(0, len(target_unit_list), chunk_size):
            batch_u_set = set(target_unit_list[c_start:c_start + chunk_size])
            for rnd in range(1, 5):
                if rnd == 1 and c_start == 0:
                    secs_state = secs_init
                else:
                    secs_state = None
                    for _ in range(3):
                        try:
                            rc = self.session.get(
                                f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse{{pathSectioned{{index,type,units{{unitIndex,levels{{state}}}}}}}}&_={int(time.time()*1000)}",
                                headers=web_headers,
                                timeout=18
                            )
                            if rc.status_code == 200:
                                secs_state = rc.json().get("currentCourse", {}).get("pathSectioned", [])
                                break
                            time.sleep(0.4)
                        except Exception:
                            time.sleep(0.4)
                    if not secs_state:
                        break

                pending_keys = []
                for s in secs_state:
                    if s.get("type") != "learning":
                        continue
                    for u in s.get("units", []):
                        u_idx = u.get("unitIndex")
                        if u_idx not in batch_u_set:
                            continue
                        for l_idx, l in enumerate(u.get("levels", [])):
                            if (u_idx, l_idx) in node_map and l.get("state") == "passed":
                                pending_keys.append((u_idx, l_idx))

                if not pending_keys:
                    break

                done_in_round = [0]
                round_total = len(pending_keys)
                t_rnd = time.time()

                def solve_leg_node(key):
                    u_idx, l_idx = key
                    lvl = node_map[key]
                    ltype = lvl.get("type")
                    subtype = lvl.get("subtype")
                    cdata = lvl.get("pathLevelClientData", {}) or {}
                    meta = lvl.get("pathLevelMetadata", {}) or {}
                    sess = self.get_worker_session() if rnd == 1 else self.session
                    wh = self._get_web_headers()
                    step_iv = 0.52 if rnd == 1 else 0.05
                    ok_node = False

                    if ltype == "story":
                        story_id = cdata.get("storyId") or meta.get("storyId")
                        for attempt in range(4):
                            pace_gateway(step_iv)
                            now = int(time.time())
                            spl = {
                                "awardXp": True,
                                "completedBonusChallenge": True,
                                "courseId": course_id,
                                "dailyRefreshInfo": lvl.get("dailyRefreshInfo"),
                                "fromLanguage": f_lang,
                                "learningLanguage": l_lang,
                                "hasXpBoost": False,
                                "illustrationFormat": "svg",
                                "isFeaturedStoryInPracticeHub": False,
                                "isLegendaryMode": True,
                                "isV2Redo": False,
                                "isV2Story": True,
                                "levelId": lvl.get("id"),
                                "levelSessionIndex": 0,
                                "masterVersion": True,
                                "maxScore": 5,
                                "mode": (cdata.get("mode") or meta.get("mode") or "READ").upper(),
                                "pathLevelSpecifics": meta,
                                "score": 5,
                                "startTime": now - 60,
                                "endTime": now,
                                "treeId": tree_id,
                            }
                            try:
                                rs = sess.post(f"https://stories.duolingo.com/api2/stories/{story_id}/complete", headers=wh, json=spl, timeout=10)
                                if rs.status_code == 200:
                                    ok_node = True
                                    break
                                time.sleep(0.15 * (attempt + 1))
                            except Exception:
                                sess = self.get_worker_session(reset=True)
                    elif leg_tmpl is not None:
                        for attempt in range(4):
                            h_url = pace_gateway(step_iv)
                            fake_sid = base_sid + "".join(random.choices(string.ascii_letters + string.digits, k=6))
                            now = int(time.time())
                            sub = json.loads(json.dumps(leg_tmpl))
                            sub["id"] = fake_sid
                            sub["courseId"] = course_id
                            sub["fromLanguage"] = f_lang
                            sub["learningLanguage"] = l_lang
                            sub["dailyRefreshInfo"] = lvl.get("dailyRefreshInfo")
                            sub["pathLevelSpecifics"] = meta
                            sub["failed"] = False
                            sub["heartsLeft"] = 0
                            sub["enableBonusPoints"] = False
                            sub["maxInLessonStreak"] = 15
                            sub["shouldLearnThings"] = True
                            sub["startTime"] = now - 60
                            sub["endTime"] = now

                            if ltype == "skill":
                                sk_id = cdata.get("skillId") or meta.get("skillId")
                                lvl_i = cdata.get("crownLevelIndex", 0)
                                sub["type"] = "LEGENDARY_LEVEL"
                                sub["skillId"] = sk_id
                                sub["levelIndex"] = lvl_i
                                if isinstance(sub.get("metadata"), dict):
                                    sub["metadata"]["id"] = fake_sid
                                    sub["metadata"]["skill_id"] = sk_id
                                    sub["metadata"]["level_index"] = lvl_i
                                if isinstance(sub.get("trackingProperties"), dict):
                                    sub["trackingProperties"]["session_id"] = fake_sid
                                    sub["trackingProperties"]["skill_id"] = sk_id
                                    sub["trackingProperties"]["level_index"] = lvl_i
                                    sub["trackingProperties"]["level_id"] = lvl.get("id")
                            else:
                                s_ids = cdata.get("skillIds", []) or cdata.get("spacedRepetitionSkillIds", [])
                                sub["type"] = "LEGENDARY_UNIT_PRACTICE" if subtype == "unit_practice" else "LEGENDARY_LEXEME_PRACTICE"
                                sub.pop("skillId", None)
                                sub.pop("levelIndex", None)
                                sub["skillIds"] = s_ids
                                if isinstance(sub.get("metadata"), dict):
                                    sub["metadata"]["id"] = fake_sid
                                    sub["metadata"]["type"] = sub["type"].lower()
                                if isinstance(sub.get("trackingProperties"), dict):
                                    sub["trackingProperties"]["session_id"] = fake_sid
                                    sub["trackingProperties"]["level_id"] = lvl.get("id")

                            try:
                                rput = sess.put(f"{h_url}/2017-06-30/sessions/{fake_sid}?fields=id", headers=wh, json=sub, timeout=10)
                                if rput.status_code == 200:
                                    ok_node = True
                                    break
                                time.sleep(0.15 * (attempt + 1))
                            except Exception:
                                sess = self.get_worker_session(reset=True)

                    with pace_lock:
                        done_in_round[0] += 1
                        cur_d = done_in_round[0]
                        if cur_d % 8 == 0 or cur_d == round_total:
                            el_r = max(0.1, time.time() - t_rnd)
                            rate_r = cur_d / el_r
                            sys.stdout.write(
                                f"\r\033[K\033[93m    ➔ [Vòng {rnd}] Đang mạ vàng Unit #{u_idx + 1}: {cur_d}/{round_total} nodes (~{rate_r:.2f} nodes/s)\033[0m"
                            )
                            sys.stdout.flush()
                    return ok_node

                c_workers = 2 if rnd == 1 else 1
                with ThreadPoolExecutor(max_workers=c_workers) as ex:
                    results = list(ex.map(solve_leg_node, pending_keys))
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                hit_round = sum(1 for r_ok in results if r_ok)
                total_upgraded += hit_round

            u_first = min(batch_u_set) + 1
            u_end = max(batch_u_set) + 1
            el_tot = max(0.1, time.time() - t_course_start)
            done_u = min(c_start + chunk_size, len(target_unit_list))
            print(green(f"    [+] Đã mạ vàng 100% Legendary cụm Unit #{u_first} ➔ Unit #{u_end} ({done_u}/{len(target_unit_list)} Units | {el_tot:.1f}s)"))

        return total_upgraded

    def complete_course(self, mode="all", with_legendary=True, legendary_units=None):
        print(cyan("\n" + "=" * 60))
        print(green("   CHẾ ĐỘ VƯỢT ALL ẢI COURSE (SKIP & PASS 100% UNITS/SECTIONS)   "))
        print(cyan("=" * 60))

        web_headers = self._get_web_headers()
        try:
            r_init = self.session.get(
                f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=courses,currentCourseId,fromLanguage,learningLanguage&_={int(time.time()*1000)}",
                headers=web_headers,
                timeout=15
            )
            if r_init.status_code != 200:
                print(red(f"[-] Không thể tải thông tin khóa học (HTTP {r_init.status_code})."))
                return False
            init_data = r_init.json()
        except Exception as e:
            print(red(f"[-] Lỗi kết nối khi tải khóa học: {e}"))
            return False

        orig_course_id = init_data.get("currentCourseId")
        orig_from = init_data.get("fromLanguage", self.from_lang or "en")
        orig_learning = init_data.get("learningLanguage", self.learning_lang or "es")
        all_courses = init_data.get("courses", []) or []

        if mode == "current" or not all_courses:
            target_courses = [{
                "id": orig_course_id,
                "title": f"{orig_learning.upper()} <- {orig_from.upper()}",
                "fromLanguage": orig_from,
                "learningLanguage": orig_learning
            }]
        else:
            target_courses = all_courses

        print(cyan(f"[*] Phát hiện {len(target_courses)} khóa học cần vượt full ải (Chế độ: {mode.upper()})..."))

        for c_idx, c_info in enumerate(target_courses, 1):
            cid = c_info.get("id")
            c_title = c_info.get("title") or cid
            f_lang = c_info.get("fromLanguage", orig_from)
            l_lang = c_info.get("learningLanguage", orig_learning)

            print(gold(f"\n[*] [{c_idx}/{len(target_courses)}] Đang xử lý khóa học: {c_title} ({cid} | {f_lang} ➔ {l_lang})..."))

            # Chuyển sang khóa học cần vượt nếu khác khóa hiện tại
            if cid and (len(target_courses) > 1 or cid != orig_course_id):
                for _sw in range(3):
                    try:
                        r_sw = self.session.patch(
                            f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourseId,fromLanguage,learningLanguage",
                            headers=web_headers,
                            json={
                                "currentCourseId": cid,
                                "fromLanguage": f_lang,
                                "learningLanguage": l_lang
                            },
                            timeout=12
                        )
                        if r_sw.status_code == 200:
                            time.sleep(0.5)
                            break
                    except Exception:
                        time.sleep(0.5)

            cc_payload = None
            for _att in range(3):
                try:
                    r_cc = self.session.get(
                        f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse,currentCourseId,rewardBundles&_={int(time.time()*1000)}",
                        headers=web_headers,
                        timeout=18
                    )
                    if r_cc.status_code == 200:
                        cc_payload = r_cc.json()
                        break
                    time.sleep(0.8)
                except Exception:
                    time.sleep(0.8)
            if not cc_payload:
                print(red(f"    [-] Lỗi tải cấu trúc Path của {c_title}"))
                continue

            cc = cc_payload.get("currentCourse", {}) or {}
            course_id = cc_payload.get("currentCourseId") or cc.get("id") or cid
            tree_id = cc.get("treeId")
            secs = cc.get("pathSectioned", []) or []

            learning_secs = [s for s in secs if s.get("type") == "learning" and s.get("units")]
            if not learning_secs:
                print(gold(f"    [!] Khóa học {c_title} không có cấu trúc pathSectioned learning hoặc đã hoàn thành."))
                continue

            total_units = sum(len(s.get("units", [])) for s in learning_secs)
            done_units = sum(s.get("completedUnits", 0) for s in learning_secs)
            print(cyan(f"    ➔ Tiến độ hiện tại: {done_units}/{total_units} cửa ải (Units) trên {len(learning_secs)} chặng (Sections)."))

            # Bước 1: Nhảy vọt (Jump / Skip) 1-hit tới Unit cuối cùng của Section learning cuối cùng bằng UNIT_TEST / SECTION_TEST
            target_sec = learning_secs[-1]
            u_last = target_sec["units"][-1]
            lvl0 = u_last["levels"][0] if u_last.get("levels") else None

            if lvl0 and lvl0.get("state") in ("unit_test", "locked") and done_units < total_units - 1:
                stype = "SECTION_TEST" if target_sec.get("status") == "locked" else "UNIT_TEST"
                print(cyan(f"    [*] Đang kích hoạt 1-Hit {stype} nhảy thẳng tới Chặng cuối (Section {target_sec.get('index')}, Unit #{u_last.get('unitIndex')})..."))
                pl_skip = {
                    "challengeTypes": ["translate", "assist", "listen"],
                    "fromLanguage": f_lang,
                    "learningLanguage": l_lang,
                    "isFinalLevel": False,
                    "isRedoingPassedNode": False,
                    "isV2": True,
                    "juicy": True,
                    "smartTipsVersion": 2,
                    "treeId": tree_id,
                    "type": stype,
                    "skillIds": lvl0.get("pathLevelClientData", {}).get("skillIds", []),
                    "levelId": lvl0.get("id"),
                    "levelSessionIndex": 0,
                    "pathLevelSessionMetadata": lvl0.get("pathLevelSessionMetadata", {}),
                    "sectionIndex": target_sec.get("index"),
                    "sectionType": target_sec.get("type", "learning"),
                }
                rs = self.session.post("https://www.duolingo.com/2017-06-30/sessions", headers=web_headers, json=pl_skip, timeout=12)
                if rs.status_code != 200 and stype == "SECTION_TEST":
                    pl_skip["type"] = "UNIT_TEST"
                    rs = self.session.post("https://www.duolingo.com/2017-06-30/sessions", headers=web_headers, json=pl_skip, timeout=12)

                if rs.status_code == 200:
                    sdata = rs.json()
                    sid = sdata.get("id")
                    now = int(time.time())
                    for k in ["adaptiveChallenges", "adaptiveInterleavedChallenges", "mistakesReplacementChallenges", "elements"]:
                        sdata.pop(k, None)
                    sdata["challenges"] = []
                    sdata["courseId"] = course_id
                    sdata["dailyRefreshInfo"] = lvl0.get("dailyRefreshInfo")
                    sdata["pathLevelSpecifics"] = lvl0.get("pathLevelMetadata")
                    sdata["failed"] = False
                    sdata["heartsLeft"] = 0
                    sdata["startTime"] = now - 60
                    sdata["endTime"] = now
                    sdata["enableBonusPoints"] = False
                    sdata["maxInLessonStreak"] = 15
                    sdata["shouldLearnThings"] = True
                    rput = self.session.put(f"https://www.duolingo.com/2017-06-30/sessions/{sid}", headers=web_headers, json=sdata, timeout=12)
                    if rput.status_code == 200:
                        print(green(f"    [+] Đã vượt 1-Hit thành công {total_units - 1}/{total_units} ải! Đang dọn nốt các node cuối của Unit #{u_last.get('unitIndex')}..."))

            # Bước 2: Giải tuần tự từng Node (Level) còn lại trong Unit cuối cùng (và cả Daily Refresh nếu có)
            for step in range(25):
                try:
                    r_step = self.session.get(
                        f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse,currentCourseId,rewardBundles&_={int(time.time()*1000)}",
                        headers=web_headers,
                        timeout=15
                    )
                    if r_step.status_code != 200:
                        break
                    step_payload = r_step.json()
                except Exception:
                    break

                cc_step = step_payload.get("currentCourse", {}) or {}
                rbundles = step_payload.get("rewardBundles", []) or []
                secs_step = cc_step.get("pathSectioned", []) or []
                l_secs_step = [s for s in secs_step if s.get("type") == "learning" and s.get("units")]
                if not l_secs_step:
                    break

                u_final = l_secs_step[-1]["units"][-1]
                active_lvl = None
                for idx, l in enumerate(u_final.get("levels", [])):
                    if l.get("state") not in ("passed", "legendary"):
                        active_lvl = (idx, l, f"Unit #{u_final.get('unitIndex')}")
                        break

                # Nếu Unit learning cuối đã pass 100%, kiểm tra thêm Daily Refresh (nếu có)
                if active_lvl is None:
                    dr_secs = [s for s in secs_step if s.get("type") == "daily_refresh" and s.get("units")]
                    if dr_secs:
                        u_dr = dr_secs[-1]["units"][0]
                        for idx, l in enumerate(u_dr.get("levels", [])):
                            if l.get("state") not in ("passed", "legendary"):
                                active_lvl = (idx, l, "Daily Refresh")
                                break

                if active_lvl is None:
                    break

                idx, lvl, unit_tag = active_lvl
                ok_lvl = self._solve_single_path_level(lvl, course_id, tree_id, f_lang, l_lang, rbundles, web_headers)
                if ok_lvl:
                    print(cyan(f"    ➔ [{unit_tag}] Đã qua Node #{idx + 1} ({lvl.get('type')}/{lvl.get('subtype')})"))
                else:
                    break

            # Kiểm tra kết quả khóa học sau khi vượt
            try:
                r_end = self.session.get(
                    f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourse&_={int(time.time()*1000)}",
                    headers=web_headers,
                    timeout=15
                )
                if r_end.status_code == 200:
                    secs_end = r_end.json().get("currentCourse", {}).get("pathSectioned", []) or []
                    l_end = [s for s in secs_end if s.get("type") == "learning" and s.get("units")]
                    tot_u = sum(len(s.get("units", [])) for s in l_end)
                    done_u = sum(s.get("completedUnits", 0) for s in l_end)
                    print(green(f"    [✓] Khóa {c_title}: Đã hoàn thành {done_u}/{tot_u} Units ({len(l_end)}/{len(l_end)} Sections)!"))
                    for s in secs_end:
                        u_len = len(s.get("units", []))
                        c_u = s.get("completedUnits", 0)
                        if s.get("type") == "daily_refresh" and u_len > 0:
                            all_p = all(l.get("state") in ("passed", "legendary") for l in s["units"][0].get("levels", []))
                            if all_p:
                                c_u = u_len
                        print(green(f"        • Section {s.get('index')} ({s.get('type')}): {c_u}/{u_len} Units Completed"))
            except Exception:
                pass

            # Bước 3: Cào lên cấp Vàng Huyền Thoại (Legendary +40 XP) cho các Node trong khóa học
            if with_legendary:
                self._upgrade_course_nodes_to_legendary(
                    course_id=course_id,
                    tree_id=tree_id,
                    f_lang=f_lang,
                    l_lang=l_lang,
                    c_title=c_title,
                    web_headers=web_headers,
                    max_units=legendary_units
                )

        # Khôi phục khóa học gốc nếu đã chuyển đổi qua nhiều khóa học
        if orig_course_id and len(target_courses) > 1:
            try:
                self.session.patch(
                    f"https://www.duolingo.com/2017-06-30/users/{self.uid}?fields=currentCourseId,fromLanguage,learningLanguage",
                    headers=web_headers,
                    json={
                        "currentCourseId": orig_course_id,
                        "fromLanguage": orig_from,
                        "learningLanguage": orig_learning
                    },
                    timeout=12
                )
            except Exception:
                pass

        print(green("\n[✓] Hoàn tất Vượt All Ải Course!"))
        return True


    def cloud_boost_request(self, req_type="xp", amount=1000):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.jwt}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        
        # Cloud server giới hạn tối đa 10,000 XP / lần gửi, tự động chia nhỏ và lặp
        if req_type == "xp":
            total_needed = amount
            completed_xp = 0
            chunk_size = 10000
            print(cyan(f"\n[*] Đang gửi yêu cầu Cloud Booster (Tổng mục tiêu: {total_needed} XP, tự động chia nhỏ {chunk_size} XP/hit)..."))
            
            while completed_xp < total_needed:
                current_chunk = min(chunk_size, total_needed - completed_xp)
                payload = {
                    "type": "xp",
                    "amount": current_chunk,
                    "version": "3.1BETA.05.1",
                    "lang": "en"
                }
                print(cyan(f"[*] Đang yêu cầu Cloud bơm đợt {current_chunk} XP..."))
                try:
                    r = requests.post("https://api.duolingopro.net/request", headers=headers, json=payload, stream=True, timeout=60)
                    if r.status_code != 200:
                        print(red(f"[-] Cloud Server trả về mã lỗi: {r.status_code} ({r.text[:100]})"))
                        break
                    
                    hit_ok = False
                    for line in r.iter_lines():
                        if not line: continue
                        try:
                            s_line = line.decode("utf-8", errors="ignore").strip()
                            if not s_line: continue
                            data = json.loads(s_line)
                            if data.get("status") == "completed":
                                hit_ok = True
                                notif = data.get("notification", {})
                                print(green(f" [✓] {notif.get('head', 'Thành công')} - {notif.get('body', '')}"))
                            elif data.get("status") in ["failed", "rejected"]:
                                notif = data.get("notification", {})
                                print(gold(f" [!] Cloud Server: {notif.get('head', 'Từ chối')} ({notif.get('body', '')})"))
                                break
                        except:
                            pass
                    
                    if hit_ok:
                        completed_xp += current_chunk
                        print(green(f"[+] Tiến độ Cloud: Đã bơm {completed_xp}/{total_needed} XP"))
                    else:
                        break
                    time.sleep(1.0)
                except Exception as e:
                    print(red(f"[-] Lỗi kết nối Cloud: {e}"))
                    break
            return completed_xp > 0
        else:
            print(cyan(f"\n[*] Đang gửi yêu cầu Cloud Booster ({req_type.upper()}: {amount})..."))
            payload = {
                "type": req_type,
                "amount": amount,
                "version": "3.1BETA.05.1",
                "lang": "en"
            }
            try:
                r = requests.post("https://api.duolingopro.net/request", headers=headers, json=payload, stream=True, timeout=60)
                if r.status_code != 200:
                    print(red(f"[-] Cloud Server trả về mã lỗi: {r.status_code}"))
                    return False
                for line in r.iter_lines():
                    if not line: continue
                    try:
                        s_line = line.decode("utf-8", errors="ignore").strip()
                        if not s_line: continue
                        data = json.loads(s_line)
                        if data.get("status") == "completed":
                            notif = data.get("notification", {})
                            print(green(f"\n[✓] Cloud Booster Hoàn Tất: {notif.get('head')}! ({notif.get('body')})"))
                            return True
                    except:
                        pass
                return True
            except Exception as e:
                print(red(f"[-] Lỗi kết nối Cloud Server: {e}"))
                return False

    def follow_user(self, target_identifier):
        target_uid = None
        target_uname = None

        raw_target = str(target_identifier).strip()
        u_clean = raw_target.split("@")[0].strip()

        # Database nhận diện nhanh từ duo_hits.txt và các tài khoản đã biết
        known_map = {
            # Cập nhật chuẩn:
                "Nut592027": ("666016469003973", "Nut592027"),
                "jacksonjames8171@gmail.com": ("666016469003973", "Nut592027"),
        }
        try:
            hits_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duo_hits.txt")
            if os.path.exists(hits_path):
                with open(hits_path, "r", encoding="utf-8") as f_hits:
                    for line in f_hits:
                        m_id = re.search(r"ID:\s*(\d+)", line)
                        m_u = re.search(r"Username:\s*([^\s|]+)", line)
                        m_e = re.search(r"Email:\s*([^\s|]+)", line)
                        if m_id:
                            uid_v = m_id.group(1)
                            uname_v = m_u.group(1) if m_u else uid_v
                            if m_u:
                                known_map[m_u.group(1).lower()] = (uid_v, uname_v)
                            if m_e:
                                known_map[m_e.group(1).lower()] = (uid_v, uname_v)
                                known_map[m_e.group(1).split("@")[0].lower()] = (uid_v, uname_v)
        except Exception:
            pass

        # Check if target_identifier is numeric UID or username/email
        if raw_target.isdigit():
            target_uid = raw_target
            target_uname = raw_target
        elif raw_target.lower() in known_map:
            target_uid, _ = known_map[raw_target.lower()]
            target_uname = u_clean
        elif u_clean.lower() in known_map:
            target_uid, _ = known_map[u_clean.lower()]
            target_uname = u_clean
        else:
            # Query username or lookup
            wh = self._get_web_headers()
            try:
                r_lookup = self.session.get(f"https://www.duolingo.com/2017-06-30/users?username={u_clean}", headers=wh, timeout=12)
                if r_lookup.status_code == 200:
                    u_list = r_lookup.json().get("users", [])
                    if u_list:
                        target_uid = str(u_list[0].get("id"))
                        target_uname = u_list[0].get("username", u_clean)
            except Exception:
                pass

        if not target_uid:
            print(red(f"[-] Không tìm thấy UID của target: {target_identifier}"))
            return False, "User not found"

        # Đảm bảo tài khoản follower đã khởi tạo ít nhất 1 bài học (Duolingo yêu cầu khóa học hợp lệ để follow)
        info = self.get_info()
        if not info or not info.get("courses") or info.get("totalXp", 0) == 0:
            print(gold(f"[*] Tài khoản {self.identifier} chưa khởi tạo khóa học. Đang tự động hoàn tất 1 lesson kích hoạt profile..."))
            self.solve_session()

        sess = self.get_worker_session()
        sess.headers["Referer"] = f"https://www.duolingo.com/profile/{target_uname or target_uid}"
        sess.headers["Origin"] = "https://www.duolingo.com"
        sess.headers["Cookie"] = f"jwt_token={self.jwt}"
        body = {
            "component": "profile_header",
            "follow_reason": "profile",
            "follow_suggestion_score": None,
            "suggested_reason": None
        }

        endpoints = [
            f"https://android-api-cf.duolingo.com/2017-06-30/friends/users/{self.uid}/follow/{target_uid}",
            f"https://www.duolingo.com/2017-06-30/friends/users/{self.uid}/follow/{target_uid}"
        ]

        for url in endpoints:
            try:
                r = sess.post(url, json=body, timeout=12)
                if r.status_code in [200, 201]:
                    res_data = r.json() if r.text else {}
                    if res_data.get("successful") is not False:
                        print(green(f" [✓] {self.identifier} ➔ Follow THÀNH CÔNG target {target_uname or target_uid} (UID: {target_uid})!"))
                        return True, "Followed successful"
            except Exception as e:
                pass

        print(red(f" [-] {self.identifier} ➔ Follow thất bại target {target_uname or target_uid}"))
        return False, "Failed to follow"

    def print_summary(self):
        print(cyan("\n" + "=" * 50))
        print(green("             KẾT QUẢ HOÀN TẤT             "))
        print(cyan("=" * 50))
        final_info = self.get_info()
        if final_info:
            f_streak = final_info.get("streak", 0)
            f_data = final_info.get("streakData", {}) or {}
            f_cur = f_data.get("currentStreak") or {}
            print(green(f"[✓] Username      : {final_info.get('username')}"))
            print(green(f"[✓] Streak mới    : {f_streak} ngày"))
            print(green(f"[✓] Ngày bắt đầu  : {f_cur.get('startDate')}"))
            print(green(f"[✓] Hôm nay       : {f_cur.get('endDate')}"))
            print(green(f"[✓] Tổng XP       : {final_info.get('totalXp')}"))
            print(green(f"[✓] Gems / Đá quý : {final_info.get('gemsConfig', {}).get('gems')}"))
        print(cyan("=" * 50 + "\n"))

def main():
    if "--gui" in sys.argv or "-g" in sys.argv:
        try:
            from duo_gui import launch_gui
            launch_gui()
        except ImportError:
            print(red("[-] Không tìm thấy tệp 'duo_gui.py' trong thư mục để khởi động giao diện đồ họa."))
        return

    print(cyan("============================================================"))
    print(cyan("  DUOLINGO STREAK, XP & GEMS MULTI-BOOSTER (INTERACTIVE CLI) "))
    print(cyan("============================================================\n"))

    # 1. Chọn chế độ tính năng rõ ràng, tách bạch
    print(green("[0] 🖥️  MỞ GIAO DIỆN ĐỒ HỌA (GUI DESKTOP - Giống bản AOV VN Multi-Tool)"))
    print(gold("[1] Bơm Streak Cổ (Streak Booster - Ultra-Slim 1-PUT/ngày)"))
    print(gold("[2] CHỈ CÀY XP (XP Farm - Siêu tốc ~1,300 XP/s, Không chạm Gems & Streak)"))
    print(gold("[3] CHỈ BUFF GEMS (Gem Booster - Tự động hồi Bucket 30 rương/phút, Bơm vô hạn UUIDv3)"))
    print(gold("[4] BUFF ĐỒNG THỜI XP + GEMS (Không đụng vào Streak)"))
    print(gold("[5] Combo Max Toàn Bộ (Bơm Streak + Farm XP + Buff Gems + Badge Tháng + Thành Tựu + Vượt All Ải Course)"))
    print(gold("[6] Mở Khóa Huy Hiệu Tháng (Monthly Badges Unlocker - Đơn lẻ hoặc Full bộ từ 2021)"))
    print(gold("[7] Kích Hoạt Super Duolingo & Quà Tặng Bạn Bè (Referral Bot)"))
    print(gold("[8] Bơm XP / Buff Siêu Tốc qua Cloud Server (Duolingo-PRO Cloud Engine - 1 Hit Ăn Luôn)"))
    print(gold("[9] Mở Khóa Thành Tựu Hồ Sơ (Achievements / Awards Unlocker - Flawless Finisher, Legend...)"))
    print(gold("[10] Vượt All Ải Course + Full Vàng Legendary (Skip 100% Units/Sections & Nâng Cấp Legendary)"))
    print(gold("[11] Buff Follower Cho Acc Chính (Dùng List Clone/Acc để Follow Target UID hoặc Username)"))
    print(green("[12] 👥 MỞ GIAO DIỆN ĐỒ HỌA BUFF FOLLOWER (Follower Multi-Threaded GUI)"))
    feature_choice = input(cyan("[?] Chọn tính năng [Mặc định: 0 (Mở GUI) hoặc 4]: ")).strip()
    if feature_choice == "0":
        try:
            from duo_gui import launch_gui
            launch_gui()
        except ImportError:
            print(red("[-] Không tìm thấy tệp 'duo_gui.py'. Vui lòng chọn các tính năng CLI (1 - 11)."))
        return
    if feature_choice == "12":
        try:
            from duo_follower_gui import launch_follower_gui
            launch_follower_gui()
        except ImportError:
            print(red("[-] Không tìm thấy tệp 'duo_follower_gui.py'."))
        return
    if not feature_choice: feature_choice = "4"

    course_skip_mode = "all"
    with_legendary = True
    legendary_units = None
    if feature_choice == "10":
        print(gold("\n[1] Vượt All Ải cho TẤT CẢ các khóa học trong tài khoản (Mặc định)"))
        print(gold("[2] Chỉ vượt All Ải cho khóa học ĐANG HỌC hiện tại"))
        cs_choice = input(cyan("[?] Chọn phạm vi vượt ải [Mặc định: 1]: ")).strip()
        course_skip_mode = "current" if cs_choice == "2" else "all"

        print(gold("\n[1] Nâng cấp thêm cấp độ VÀNG HUYỀN THOẠI (Legendary Gold) cho các ải (Mặc định)"))
        print(gold("[2] Chỉ vượt ải Xanh (Passed 100%), bỏ qua Legendary để chạy siêu tốc"))
        leg_choice = input(cyan("[?] Chọn chế độ Legendary [Mặc định: 1]: ")).strip()
        with_legendary = (leg_choice != "2")
        if with_legendary:
            u_str = input(gold("[?] Số Units đầu mỗi khóa muốn cào Vàng Legendary (Enter = Toàn bộ khóa, hoặc nhập số vd 50): ")).strip()
            legendary_units = int(u_str) if u_str.isdigit() and int(u_str) > 0 else None

    # Thông số cho Monthly Badges
    badge_mode = "current"
    b_month = None
    b_year = None
    if feature_choice == "6":
        print(gold("\n[1] Mở khóa Huy Hiệu Tháng Hiện Tại"))
        print(gold("[2] Mở khóa Huy Hiệu Tháng Cụ Thể (Nhập tháng/năm, vd: 08/2026, 05/2024)"))
        print(gold("[3] Mở khóa TOÀN BỘ Huy Hiệu Tháng từ 2021 đến nay (Full Collection 60+ Badges)"))
        b_choice = input(cyan("[?] Chọn chế độ mở khóa badge [Mặc định: 1]: ")).strip()
        if b_choice == "2":
            badge_mode = "specific"
            m_input = input(gold("[?] Nhập tháng/năm dạng mm/yyyy (vd: 09/2026): ")).strip()
            try:
                parts = m_input.split("/")
                b_month = int(parts[0])
                b_year = int(parts[1])
            except:
                print(red("[-] Định dạng không đúng, mặc định dùng tháng hiện tại."))
                badge_mode = "current"
        elif b_choice == "3":
            badge_mode = "all"
        else:
            badge_mode = "current"

    # Số lượng clone cho tính năng Referral Bot
    num_referrals = 1
    if feature_choice == "7":
        ref_str = input(gold("[?] Nhập số lượt mời muốn chạy [Mặc định: 1]: ")).strip()
        num_referrals = int(ref_str) if ref_str.isdigit() and int(ref_str) > 0 else 1

    # Target cho tính năng Follower Booster (Option 11)
    follow_target = "tcook4059"
    if feature_choice == "11":
        f_in = input(gold("[?] Nhập Target muốn buff Follower (Username hoặc UID) [Mặc định: tcook4059]: ")).strip()
        if f_in:
            follow_target = f_in

    # Cấu hình cho Cloud Booster (Duolingo-PRO)
    cloud_type = "xp"
    cloud_amount = 10000
    if feature_choice == "8":
        print(gold("\n[1] Bơm XP Siêu Tốc (Mặc định: 10,000 XP/hit)"))
        print(gold("[2] Kích hoạt x2 Nhân Đôi Kinh Nghiệm (Double XP Boost)"))
        print(gold("[3] Hồi phục Full Tim (Heart Refill)"))
        print(gold("[4] Nhận Đóng Băng Chuỗi (Streak Freeze)"))
        print(gold("[5] Mở Khóa Huy Hiệu Tháng qua Cloud (Monthly Badge - 2021 đến nay)"))
        c_choice = input(cyan("[?] Chọn loại buff qua Cloud [Mặc định: 1]: ")).strip()
        if c_choice == "2":
            cloud_type = "double_xp_boost"
            cloud_amount = 1
        elif c_choice == "3":
            cloud_type = "heart_refill"
            cloud_amount = 1
        elif c_choice == "4":
            cloud_type = "streak_freeze"
            cloud_amount = 1
        elif c_choice == "5":
            cloud_type = "badge"
            b_input = input(gold("[?] Nhập tháng/năm muốn mở khóa dạng mm-yyyy (vd: 12-2021, 08-2022, 05-2026) [Mặc định: 10-2026]: ")).strip()
            cloud_amount = b_input if b_input else "10-2026"
        else:
            cloud_type = "xp"
            xp_input = input(gold("[?] Nhập số XP muốn Cloud bơm (1 - 10000) [Mặc định: 10000]: ")).strip()
            cloud_amount = int(xp_input) if xp_input.isdigit() and int(xp_input) > 0 else 10000

    # 2. Chọn chế độ nhập tài khoản
    print(gold("\n[1] Nhập 1 tài khoản trực tiếp (Email / Pass)"))
    print(gold("[2] Đọc danh sách tài khoản từ file (vd: duo_accounts.txt)"))
    choice = input(cyan("[?] Chọn nguồn tài khoản [Mặc định: 1]: ")).strip()
    if not choice: choice = "1"

    accounts = []
    if choice == "1":
        acc_str = input(gold("[?] Nhập tài khoản dạng email:pass hoặc chỉ email: ")).strip()
        if not acc_str:
            print(red("[-] Không được để trống tài khoản!"))
            return
        if ":" in acc_str:
            parts = acc_str.split(":", 1)
            u = parts[0].strip()
            p = parts[1].strip()
        else:
            u = acc_str
            p = input(gold(f"[?] Nhập mật khẩu cho {u}: ")).strip()
        accounts.append((u, p))
    else:
        file_input = input(gold("[?] Nhập đường dẫn file [Mặc định: duo_accounts.txt]: ")).strip()
        if not file_input: file_input = "duo_accounts.txt"
        if not os.path.exists(file_input):
            print(red(f"[-] File {file_input} không tồn tại!"))
            return
        with open(file_input, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                parts = line.split(":")
                if len(parts) >= 2:
                    email = parts[0].strip()
                    pw_part = ":".join(parts[1:])
                    pw = pw_part.split(']')[0].split('|')[0].strip()
                    accounts.append((email, pw))
        print(green(f"[+] Đã tải {len(accounts)} tài khoản từ file {file_input}"))

    # 3. Thông số mục tiêu (CỘNG DỒN)
    add_days = 100
    add_xp = 2000
    add_gems = 4000
    batch_size = 50

    if feature_choice in ["1", "5"]:
        days_str = input(gold("[?] Nhập số ngày streak muốn BƠM THÊM (Cộng dồn vào streak hiện tại) [Mặc định: 100]: ")).strip()
        add_days = int(days_str) if days_str.isdigit() else 100
        batch_str = input(gold("[?] Nhập kích thước Batch ngày/lượt [Mặc định: 50, kiến nghị 50-100 để đẩy siêu nhanh]: ")).strip()
        batch_size = int(batch_str) if batch_str.isdigit() else 50
    
    xp_engine = "local"
    if feature_choice in ["2", "4", "5"]:
        xp_str = input(gold("[?] Nhập số XP muốn CÀY THÊM (Cộng dồn vào XP hiện tại, vd: 2000, 5000, 10000) [Mặc định: 2000]: ")).strip()
        add_xp = int(xp_str) if xp_str.isdigit() else 2000
        
        print(gold("\n  [1] Dùng Cloud Engine (Siêu tốc 1-hit 10,000 XP/lần - Công nghệ Duolingo-PRO)"))
        print(gold("  [2] Dùng Local Turbo Pool (Chạy trực tiếp qua Stories/Sessions API ~499 XP/hit đa luồng)"))
        eng_choice = input(cyan("  [?] Chọn Engine cày XP [Mặc định: 2]: ")).strip()
        if eng_choice == "1":
            xp_engine = "cloud"
        else:
            xp_engine = "local"
            batch_str = input(gold("[?] Nhập kích thước Batch hits/lượt cày XP [Mặc định: 50, kiến nghị 50-100]: ")).strip()
            batch_size = int(batch_str) if batch_str.isdigit() else 50

    if feature_choice in ["3", "4", "5"]:
        gems_str = input(gold("[?] Nhập số Gems muốn BƠM THÊM (Tự động hồi Bucket ~400 Gems/phút, bơm bao nhiêu cũng được) [Mặc định: 4000]: ")).strip()
        add_gems = int(gems_str) if gems_str.isdigit() and int(gems_str) > 0 else 4000

    # 4. Số luồng đồng thời
    workers_str = input(gold("[?] Nhập số luồng (workers) [Mặc định: 15, nếu dùng proxy xoay có thể đặt 20-30]: ")).strip()
    workers = int(workers_str) if workers_str.isdigit() else 15

    # 5. Proxy (tùy chọn)
    config_proxy = ""
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                cfg = json.load(f)
            config_proxy = cfg.get("proxy", "").strip()
        except:
            pass

    proxy_prompt = f"[?] Nhập Proxy (host:port:user:pass, hoặc file proxies.txt, hoặc cách nhau bởi dấu phẩy"
    if config_proxy:
        proxy_prompt += f", hoặc dùng '{config_proxy}'"
    proxy_prompt += "): "
    
    proxy_input = input(gold(proxy_prompt)).strip()
    if not proxy_input and config_proxy:
        proxy_input = config_proxy

    proxies = None
    if proxy_input:
        if os.path.exists(proxy_input):
            proxies = proxy_input
            print(cyan(f"[+] Đã nạp Proxy Pool từ tệp tin: {proxy_input}"))
        elif "," in proxy_input:
            proxies = [p.strip() for p in proxy_input.split(",") if p.strip()]
            print(cyan(f"[+] Đã nạp {len(proxies)} Proxy xoay vòng!"))
        else:
            proxies = proxy_input
            print(cyan(f"[+] Đang dùng proxy: {proxies}"))
    else:
        print(gold("[!] Không dùng proxy (Direct IP)"))

    print(cyan("\n[*] Bắt đầu xử lý...\n"))

    for email, password in accounts:
        print(cyan("------------------------------------------------------------"))
        print(cyan(f"[*] Đang xử lý: {email}"))
        print(cyan("------------------------------------------------------------"))
        
        booster = DuoBooster(email, password, proxy=proxies)
        ok, res = booster.login()
        if not ok:
            print(red(f"[-] Đăng nhập thất bại ({email}): {res}"))
            continue
        
        print(green(f"[+] Đăng nhập thành công! UID: {booster.uid}"))
        
        # 1. Bơm Streak (Cộng dồn)
        if feature_choice in ["1", "5"]:
            booster.boost_streak(add_days=add_days, workers=workers, batch_size=batch_size)
        
        # 2. CHỈ CÀY XP (Hoặc trong Combo 4, 5)
        if feature_choice in ["2", "4", "5"]:
            if xp_engine == "cloud":
                booster.cloud_boost_request(req_type="xp", amount=add_xp)
            else:
                booster.boost_xp(add_xp=add_xp, workers=workers, batch_size=batch_size, with_gems=(feature_choice != "2"))

        # 3. CHỈ BUFF GEMS (Hoặc trong Combo 4, 5)
        if feature_choice in ["3", "4", "5"]:
            booster.boost_gems(target_add_gems=add_gems)

        # 4. Mở khóa Huy Hiệu Tháng (Monthly Badges)
        if feature_choice in ["5", "6"]:
            booster.unlock_monthly_badges(
                mode=badge_mode if feature_choice == "6" else "current",
                target_month=b_month,
                target_year=b_year
            )

        # 5. Kích hoạt Super Duolingo & Quà Tặng Bạn Bè (Referral Bot)
        if feature_choice == "7":
            booster.trigger_referral_bot(num_clones=num_referrals)
            booster.claim_gems()

        # 6. Bơm Siêu Tốc qua Cloud Server (Duolingo-PRO Cloud Engine)
        if feature_choice == "8":
            booster.cloud_boost_request(req_type=cloud_type, amount=cloud_amount)

        # 7. Mở khóa Thành Tựu Hồ Sơ (Achievements / Awards Unlocker)
        if feature_choice in ["5", "9"]:
            booster.unlock_achievements(workers=workers)

        # 8. Vượt All Ải Course (Skip & Pass 100% Units/Sections + Legendary Gold)
        if feature_choice in ["5", "10"]:
            booster.complete_course(
                mode=course_skip_mode if feature_choice == "10" else "all",
                with_legendary=with_legendary if feature_choice == "10" else True,
                legendary_units=legendary_units if feature_choice == "10" else 50
            )

        # 9. Buff Follower cho Target Account
        if feature_choice == "11":
            booster.follow_user(follow_target)

        booster.print_summary()

    print(green("\n[*] Đã hoàn tất toàn bộ tiến trình!"))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(gold("\n[!] Đã dừng script theo yêu cầu người dùng (Ctrl+C)."))
        sys.exit(0)
