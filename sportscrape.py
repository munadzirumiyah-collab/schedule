#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SportScrape - Scraper Jadwal Pertandingan Olahraga Harian
=========================================================

Scraper jadwal olahraga (Soccer, Basketball, NFL) dari FlashScore.com
dengan fallback ke TheSportsDB API dan Bing Search.

Mode penggunaan:
  1. Lokal:         python3 sportscrape.py
  2. GitHub Actions: otomatis via workflow (lihat .github/workflows/scrape.yml)
  3. Output custom:  python3 sportscrape.py --output /path/to/output.json
  4. Upload:         python3 sportscrape.py --upload

Sumber data (prioritas):
  1. FlashScore.com  (utama - Playwright headless browser)
  2. TheSportsDB API (fallback - free API)
  3. Bing Search     (fallback terakhir)

Output: JSON file dengan jadwal pertandingan harian
"""

import os
import sys
import json
import re
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

WIB = timezone(timedelta(hours=7))

# Output file - bisa di-override via --output atau env var
DEFAULT_OUTPUT = os.environ.get("OUTPUT_FILE", "jadwal_hari_ini.json")


# ================================================================
#  LOGGING
# ================================================================

def log(msg):
    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now} WIB] {msg}"
    print(line, flush=True)


# ================================================================
#  SCRAPER (FlashScore + Multi-source)
# ================================================================

class GoogleSportsScraper:
    def __init__(self, output_file=None):
        self.output_filename = output_file or DEFAULT_OUTPUT
        self.polite_delay = 2.0

    FLASHSCORE_URLS = {
        "Soccer": "https://www.flashscore.com/football/",
        "Basketball": "https://www.flashscore.com/basketball/",
        "NFL": "https://www.flashscore.com/american-football/usa/nfl/",
    }

    FLASHSCORE_ID_PREFIX = {
        "Soccer": "g_1_",
        "Basketball": "g_2_",
        "NFL": "g_4_",
    }

    def _create_browser(self, playwright):
        browser = playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled',
                  '--disable-dev-shm-usage', '--disable-gpu']
        )
        context = browser.new_context(
            locale='en-US',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/126.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
            timezone_id='Asia/Jakarta',
        )
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'id-ID', 'id']});
            window.chrome = {runtime: {}};
        """)
        return browser, page

    def _fetch_html(self, url, page, wait_ms=5000):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(wait_ms)
            return page.content()
        except Exception as e:
            log(f"[ERROR] Gagal fetch {url[:60]}...: {e}")
            return None

    def _fallback_data(self, sport_type):
        now = datetime.now(WIB)
        today_str = now.strftime('%Y-%m-%d')
        if sport_type == "Soccer":
            return [
                {"sport": "Soccer", "competition": "Demo Data",
                 "match_date": today_str, "match_time": "22:00 WIB",
                 "match_datetime": f"{today_str}T22:00:00+07:00",
                 "home_team": "Manchester City", "away_team": "Chelsea",
                 "home_logo": "https://www.thesportsdb.com/images/media/team/badge/xtwxyt1421553225.png",
                 "away_logo": "https://www.thesportsdb.com/images/media/team/badge/yuwvtu1421552653.png",
                 "is_real": False},
                {"sport": "Soccer", "competition": "Demo Data",
                 "match_date": today_str, "match_time": "01:45 WIB",
                 "match_datetime": f"{today_str}T01:45:00+07:00",
                 "home_team": "Real Madrid", "away_team": "Atletico Madrid",
                 "home_logo": "https://www.thesportsdb.com/images/media/team/badge/xwqytr1421553061.png",
                 "away_logo": "https://www.thesportsdb.com/images/media/team/badge/0j55y41568846543.png",
                 "is_real": False},
            ]
        elif sport_type == "Basketball":
            return [
                {"sport": "Basketball", "competition": "Demo Data (Off Season)",
                 "match_date": today_str, "match_time": "08:30 WIB",
                 "match_datetime": f"{today_str}T08:30:00+07:00",
                 "home_team": "Golden State Warriors", "away_team": "Boston Celtics",
                 "home_logo": "https://www.thesportsdb.com/images/media/team/badge/qxxjyx1421553225.png",
                 "away_logo": "https://www.thesportsdb.com/images/media/team/badge/0j55y41568846543.png",
                 "is_real": False},
            ]
        elif sport_type == "NFL":
            return [
                {"sport": "NFL", "competition": "Demo Data (Off Season)",
                 "match_date": today_str, "match_time": "06:15 WIB",
                 "match_datetime": f"{today_str}T06:15:00+07:00",
                 "home_team": "Kansas City Chiefs", "away_team": "San Francisco 49ers",
                 "home_logo": "https://www.thesportsdb.com/images/media/team/badge/qxxjyx1421553225.png",
                 "away_logo": "https://www.thesportsdb.com/images/media/team/badge/xwqytr1421553061.png",
                 "is_real": False},
            ]
        return []

    # --- FlashScore Parser ---

    def _parse_competition_name(self, comp_text):
        """Parse nama kompetisi dan country dari teks header FlashScore.

        Format: "World Championship WORLD: Standings"
        Returns: (competition_name, country_code)
        """
        parts = comp_text.split(':')
        if parts:
            comp_name = parts[0].strip()
            words = comp_name.split()
            if len(words) >= 2:
                if words[-1].isupper() and len(words[-1]) >= 2:
                    country = words[-1]
                    comp = ' '.join(words[:-1])
                    return comp, country
            return comp_name, ""
        return "Tournament", ""

    def _clean_match_time(self, raw_time):
        """Parse waktu match dan return (display_time, is_following_day)."""
        t = raw_time.strip()
        is_fro = False

        if t.lower() == 'finished': return 'FT', False
        elif t.lower() == 'postponed': return 'Postponed', False
        elif t.lower() == 'cancelled': return 'Cancelled', False
        elif t.lower() == 'abandoned': return 'Abandoned', False
        elif t.lower() in ('awaiting updates', 'tba'): return 'TBA', False
        elif re.match(r'^\d{1,2}:\d{2}$', t): return t + ' WIB', False
        elif re.match(r'^\d{1,3}(\+\d+)?$', t):
            try:
                minute = int(t.split('+')[0])
                if minute <= 120: return f"LIVE ({t}')", False
            except ValueError: pass
            return t, False
        elif 'FRO' in t:
            is_fro = True
            clean_t = t.replace('FRO', '').strip()
            if re.match(r'^\d{1,2}:\d{2}$', clean_t):
                return clean_t + ' WIB', True
            return clean_t + ' WIB', True
        elif 'live' in t.lower(): return 'LIVE', False
        return t, False

    def _extract_flashscore_match(self, div, sport_type, id_prefix, competition,
                                  country="", today_str=None, tomorrow_str=None):
        el_id = div.get('id', '')
        if not el_id.startswith(id_prefix): return None

        home_span = div.find('div', class_=re.compile(r'event__homeParticipant', re.I))
        away_span = div.find('div', class_=re.compile(r'event__awayParticipant', re.I))
        if not home_span:
            home_span = div.find('div', class_=re.compile(r'event__participant--home', re.I))
        if not away_span:
            away_span = div.find('div', class_=re.compile(r'event__participant--away', re.I))

        home = home_span.get_text(strip=True) if home_span else ''
        away = away_span.get_text(strip=True) if away_span else ''
        if not home or not away: return None

        time_div = div.find('div', class_=re.compile(r'event__time', re.I))
        stage_div = div.find('div', class_=re.compile(r'event__stage', re.I))
        match_time = "TBA"
        is_fro = False
        if time_div:
            match_time, is_fro = self._clean_match_time(time_div.get_text(strip=True))
        elif stage_div:
            match_time, is_fro = self._clean_match_time(stage_div.get_text(strip=True))

        # Tentukan tanggal match
        match_date = tomorrow_str if is_fro else today_str

        # Buat datetime ISO jika memungkinkan
        match_datetime = ""
        time_match = re.match(r'(\d{1,2}):(\d{2})', match_time)
        if time_match and match_date:
            hh, mm = time_match.group(1), time_match.group(2)
            match_datetime = f"{match_date}T{hh.zfill(2)}:{mm}:00+07:00"

        # Extract logo URLs dari <img> tags di dalam match div
        home_logo = ""
        away_logo = ""
        imgs = div.find_all('img')
        if len(imgs) >= 2:
            home_logo = imgs[0].get('src', '')
            away_logo = imgs[1].get('src', '')
        elif len(imgs) == 1:
            home_logo = imgs[0].get('src', '')

        # Extract skor dari full text
        score_home = ""
        score_away = ""
        full_text = div.get_text(separator='|', strip=True)
        parts = full_text.split('|')
        if len(parts) >= 2:
            last2 = parts[-2:]
            if (re.match(r'^\d{1,2}$', last2[0]) and re.match(r'^\d{1,2}$', last2[1])
                    and match_time in ('FT', 'LIVE', 'AET', 'AP')):
                score_home = last2[0]
                score_away = last2[1]
            elif last2[0] == '-' and last2[1] == '-':
                score_home = ""
                score_away = ""

        result = {
            "sport": sport_type, "competition": competition,
            "match_date": match_date or "",
            "match_time": match_time,
            "match_datetime": match_datetime,
            "home_team": home, "away_team": away,
            "home_logo": home_logo, "away_logo": away_logo,
            "is_real": True,
        }
        if country:
            result["country"] = country
        if score_home != "" and score_away != "":
            result["score_home"] = score_home
            result["score_away"] = score_away

        return result

    def _parse_flashscore(self, html, sport_type, id_prefix):
        matches = []
        try:
            now = datetime.now(WIB)
            today_str = now.strftime('%Y-%m-%d')
            tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')

            soup = BeautifulSoup(html, 'html.parser')
            match_divs = soup.find_all(attrs={'id': re.compile(f'^{re.escape(id_prefix)}')})
            if not match_divs:
                log(f"[FlashScore] Tidak ada match divs untuk {sport_type}")
                return []

            sport_sections = soup.find_all('div', class_=re.compile(r'sportName'))
            current_competition = "Tournament"
            current_country = ""

            for sport_section in sport_sections:
                section_classes = ' '.join(sport_section.get('class', []))
                if 'sportNews' in section_classes: continue

                for child in sport_section.children:
                    if not hasattr(child, 'get'): continue
                    child_classes = ' '.join(child.get('class', []))
                    if 'headerLeague' in child_classes:
                        current_competition, current_country = self._parse_competition_name(
                            child.get_text(separator=' ', strip=True))
                        continue
                    if 'event__match' in child_classes:
                        match = self._extract_flashscore_match(
                            child, sport_type, id_prefix, current_competition, current_country,
                            today_str=today_str, tomorrow_str=tomorrow_str)
                        if match: matches.append(match)

            if not matches:
                for div in match_divs:
                    prev = div.find_previous_sibling(class_=re.compile(r'headerLeague', re.I))
                    if prev:
                        competition, country = self._parse_competition_name(
                            prev.get_text(separator=' ', strip=True))
                    else:
                        competition, country = "Tournament", ""
                    match = self._extract_flashscore_match(
                        div, sport_type, id_prefix, competition, country,
                        today_str=today_str, tomorrow_str=tomorrow_str)
                    if match: matches.append(match)
        except Exception as e:
            log(f"[ERROR] Parsing FlashScore gagal: {e}")
        return matches

    # --- Bing Search ---

    def _scrape_bing(self, sport_type, query, page):
        encoded = urllib.parse.quote(query)
        url = f"https://www.bing.com/search?q={encoded}&setlang=en-US&cc=us"
        html = self._fetch_html(url, page, wait_ms=3000)
        if not html: return []
        matches = []
        try:
            soup = BeautifulSoup(html, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)
            vs_matches = re.findall(
                r'([A-Z][A-Za-z\s\.]{2,30}?)\s+vs\.?\s+([A-Z][A-Za-z\s\.]{2,30})', text)
            for home, away in vs_matches:
                home, away = home.strip(), away.strip()
                if (home != away and 3 < len(home) < 35 and 3 < len(away) < 35
                        and not home.isdigit() and not away.isdigit()):
                    if not any(m['home_team'] == home and m['away_team'] == away for m in matches):
                        matches.append({"sport": sport_type, "competition": "Tournament",
                                        "match_date": datetime.now(WIB).strftime('%Y-%m-%d'),
                                        "match_time": "TBA", "match_datetime": "",
                                        "home_team": home, "away_team": away,
                                        "home_logo": "", "away_logo": "",
                                        "is_real": True})
        except Exception as e:
            log(f"[ERROR] Parsing Bing gagal: {e}")
        return matches

    # --- TheSportsDB ---

    def _scrape_thesportsdb(self, sport_type, page):
        league_ids = {
            "Soccer": [4328, 4335, 4332, 4331, 4334],
            "Basketball": [4387], "NFL": [4391],
        }
        ids = league_ids.get(sport_type, [])
        matches = []
        today_str = datetime.now(WIB).strftime("%Y-%m-%d")
        for lid in ids:
            api_url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={today_str}&l={lid}"
            html = self._fetch_html(api_url, page, wait_ms=1500)
            if not html: continue
            try:
                soup = BeautifulSoup(html, 'html.parser')
                pre = soup.find('pre')
                if not pre: continue
                data = json.loads(pre.text)
                events = data.get('events')
                if not events: continue
                for event in events:
                    home = event.get('strHomeTeam', '').strip()
                    away = event.get('strAwayTeam', '').strip()
                    comp = event.get('strLeague', 'Tournament')
                    time_str = event.get('strTime', '')
                    match_time = "TBA"
                    if time_str:
                        try:
                            utc_time = datetime.strptime(time_str, "%H:%M:%S")
                            wib_time = utc_time + timedelta(hours=7)
                            match_time = wib_time.strftime("%H:%M") + " WIB"
                        except Exception: match_time = time_str[:5]
                    if home and away:
                        if not any(m['home_team'] == home and m['away_team'] == away for m in matches):
                            home_badge = event.get('strHomeTeamBadge', '') or ''
                            away_badge = event.get('strAwayTeamBadge', '') or ''
                            evt_date = event.get('dateEvent', today_str) or today_str
                            match_datetime = ""
                            tm = re.match(r'(\d{1,2}):(\d{2})', match_time)
                            if tm and evt_date:
                                match_datetime = f"{evt_date}T{tm.group(1).zfill(2)}:{tm.group(2)}:00+07:00"
                            matches.append({"sport": sport_type, "competition": comp,
                                            "match_date": evt_date, "match_time": match_time,
                                            "match_datetime": match_datetime,
                                            "home_team": home, "away_team": away,
                                            "home_logo": home_badge, "away_logo": away_badge,
                                            "is_real": True})
            except Exception: continue
        return matches

    # --- Orchestrator ---

    def _scrape_sport(self, sport_type, page):
        bing_queries = {
            "Soccer": "soccer fixtures today results",
            "Basketball": "nba basketball games today",
            "NFL": "nfl football games today schedule",
        }

        # FlashScore
        log(f"[1/3] FlashScore untuk {sport_type}...")
        url = self.FLASHSCORE_URLS.get(sport_type)
        id_prefix = self.FLASHSCORE_ID_PREFIX.get(sport_type, "g_1_")
        if url:
            html = self._fetch_html(url, page, wait_ms=6000)
            if html:
                fs_matches = self._parse_flashscore(html, sport_type, id_prefix)
                if fs_matches:
                    log(f"  -> FlashScore: {len(fs_matches)} pertandingan")
                    return fs_matches
        log(f"  -> FlashScore: tidak ada data")

        # TheSportsDB
        log(f"[2/3] TheSportsDB untuk {sport_type}...")
        tsdb = self._scrape_thesportsdb(sport_type, page)
        if tsdb:
            log(f"  -> TheSportsDB: {len(tsdb)} pertandingan")
            return tsdb
        log(f"  -> TheSportsDB: tidak ada data")

        # Bing
        log(f"[3/3] Bing Search untuk {sport_type}...")
        query = bing_queries.get(sport_type, f"{sport_type.lower()} schedule today")
        bing = self._scrape_bing(sport_type, query, page)
        if bing:
            log(f"  -> Bing: {len(bing)} pertandingan")
            return bing
        log(f"  -> Bing: tidak ada data")

        log(f"[FALLBACK] Semua sumber gagal untuk {sport_type}")
        return self._fallback_data(sport_type)

    # --- League Priority ---

    POPULAR_COMPETITIONS = [
        'World Championship', 'World Cup', 'Copa America', 'Euro',
        'Champions League', 'Europa League', 'Conference League',
        'Serie B',  # Brazil
        'Copa Libertadores', 'Copa Sudamericana',
        'Copa de la Liga',
        'NBA', 'NFL', 'MLB', 'NHL',
    ]

    POPULAR_COMPETITIONS_EXACT = {
        'Premier League': ['England', 'ENGLAND'],
        'La Liga': ['Spain', 'SPAIN'],
        'Serie A': ['Italy', 'ITALY', 'Brazil', 'BRAZIL'],
        'Bundesliga': ['Germany', 'GERMANY'],
        'Ligue 1': ['France', 'FRANCE'],
        'Eredivisie': ['Netherlands', 'NETHERLANDS'],
        'Primeira Liga': ['Portugal', 'PORTUGAL'],
        'Liga MX': ['Mexico', 'MEXICO'],
        'MLS': ['USA', 'United States'],
        'Super Lig': ['Turkey', 'TURKEY'],
        'J-League': ['Japan', 'JAPAN'],
        'K-League': ['South Korea', 'KOREA'],
        'FA Cup': ['England', 'ENGLAND'],
        'Copa del Rey': ['Spain', 'SPAIN'],
        'DFB-Pokal': ['Germany', 'GERMANY'],
        'AFC Champions League': ['AFC', 'ASIA'],
    }

    def _is_popular(self, match):
        """Cek apakah pertandingan termasuk liga populer."""
        competition = match.get('competition', '').strip()
        country = match.get('country', '').strip()

        for pop in self.POPULAR_COMPETITIONS:
            if pop.lower() in competition.lower():
                return True

        for exact_name, valid_countries in self.POPULAR_COMPETITIONS_EXACT.items():
            if competition.lower() == exact_name.lower():
                if not valid_countries:
                    return True
                if country.upper() in [c.upper() for c in valid_countries]:
                    return True
                return False

        return False

    # --- Main Run ---

    def run(self):
        now = datetime.now(WIB)
        log(f"[START] Scraping jadwal olahraga ({now.strftime('%Y-%m-%d %H:%M')} WIB)...")
        all_matches = []

        with sync_playwright() as p:
            browser, page = self._create_browser(p)
            for sport_type in ["Soccer", "Basketball", "NFL"]:
                matches = self._scrape_sport(sport_type, page)
                all_matches.extend(matches)
                time.sleep(self.polite_delay)
            browser.close()

        # Pisahkan: popular matches vs other matches
        popular = [m for m in all_matches if self._is_popular(m) and m.get('is_real', False)]
        other = [m for m in all_matches if not self._is_popular(m) and m.get('is_real', False)]
        demo = [m for m in all_matches if not m.get('is_real', False)]

        output = {
            "last_updated": now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "WIB (UTC+7)",
            "total_matches": len(all_matches),
            "popular_matches": len(popular),
            "data": {
                "highlight": popular,
                "other": other,
                "demo": demo,
            }
        }

        # Write JSON
        try:
            with open(self.output_filename, 'w', encoding='utf-8') as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            log(f"[SUCCESS] {len(all_matches)} pertandingan "
                f"({len(popular)} popular, {len(other)} other, {len(demo)} demo)")
            log(f"[OUTPUT] {self.output_filename}")
        except Exception as e:
            log(f"[ERROR] Gagal menulis file: {e}")

        return output

    def upload_to_tmpfiles(self):
        """Upload JSON output ke tmpfiles.org."""
        import urllib.request
        if not os.path.exists(self.output_filename):
            log("[ERROR] File output tidak ditemukan!")
            return None
        try:
            with open(self.output_filename, 'rb') as f:
                file_data = f.read()
            boundary = '----FormBoundary7MA4YWxkTrZu0gW'
            filename = os.path.basename(self.output_filename)
            body = (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f'Content-Type: application/json\r\n\r\n'
            ).encode() + file_data + f'\r\n--{boundary}--\r\n'.encode()
            req = urllib.request.Request(
                'https://tmpfiles.org/api/v1/upload',
                data=body,
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            if result.get('status') == 'success':
                view_url = result['data']['url']
                dl_url = view_url.replace('tmpfiles.org/', 'tmpfiles.org/dl/')
                log(f"[UPLOAD] Viewer: {view_url}")
                log(f"[UPLOAD] Direct: {dl_url}")
                return dl_url
            else:
                log(f"[ERROR] Upload gagal: {result}")
                return None
        except Exception as e:
            log(f"[ERROR] Upload gagal: {e}")
            return None


# ================================================================
#  MAIN
# ================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='SportScrape - Jadwal Olahraga Harian')
    parser.add_argument('--output', '-o', default=None,
                        help='Output file path (default: jadwal_hari_ini.json)')
    parser.add_argument('--upload', '-u', action='store_true',
                        help='Upload JSON ke tmpfiles.org setelah scraping')
    parser.add_argument('--upload-only', action='store_true',
                        help='Upload file yang sudah ada (tanpa scraping ulang)')
    args = parser.parse_args()

    scraper = GoogleSportsScraper(output_file=args.output)

    if args.upload_only:
        scraper.upload_to_tmpfiles()
        return

    result = scraper.run()

    if args.upload:
        scraper.upload_to_tmpfiles()

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
