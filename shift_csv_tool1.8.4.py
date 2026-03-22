import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog
import csv
import re
import os
import calendar
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, date as date_cls
from typing import List, Dict, Tuple, Optional, Set

# --- UI / Google API 関連 ---
try:
    import ttkbootstrap as ttkb
    from ttkbootstrap import ttk
    HAS_TTKB = True
except Exception:
    from tkinter import ttk
    HAS_TTKB = False

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

# --- 定数設定 ---
__app_name__ = "Smart Shift Sync"
__version__ = "1.8.4"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
SETTINGS_FILE = "shift_settings.json"
TOKEN_FILE = "token.json"

CLIENT_CONFIG = {
    "installed": {
        "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
        "client_secret": "YOUR_CLIENT_SECRET",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"]
    }
}

MD_COLORS = {
    "primary": "#6750A4",
    "onPrimary": "#FFFFFF",
    "secondary": "#03DAC6",
    "error": "#B3261E",
    "surface": "#FFFFFF",
    "workdayFill": "#E7F5EC",
    "limitFill": "#FFDAD6",
}

# --- 勤務ポータル接続エンジン ---
class PortalScraper:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://crew-p.usj.co.jp"
        self.login_post_url = f"{self.base_url}/cws/mbl/MblActLogin@act=submit"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, Gecko) Version/15.0 Mobile Safari",
            "Referer": f"{self.base_url}/cws/mbl/MblActLogin",
        }

    def login(self, user_id, password):
        payload = {"user_id": str(user_id), "password": str(password), "submit": " 　Login　 "}
        try:
            res = self.session.post(self.login_post_url, data=payload, headers=self.headers, timeout=10)
            res.encoding = "shift_jis"
            return "メインメニュー" in res.text or "main menu" in res.text
        except: return False

    def get_month_options(self) -> List[Dict]:
        confirm_menu_url = f"{self.base_url}/cws/mbl/MblActSftReqSftConfirm"
        try:
            res = self.session.get(confirm_menu_url, headers=self.headers, timeout=10)
            res.encoding = "shift_jis"
            soup = BeautifulSoup(res.text, "html.parser")
            options = []
            for a in soup.find_all("a"):
                text = a.text.replace("\xa0", " ").strip()
                if re.search(r"\d{1,2}/\d{1,2}", text) and "-" in text:
                    href = a.get("href")
                    url = f"{self.base_url}/cws/mbl/{href}" if not href.startswith("http") else href
                    options.append({"label": text, "url": url})
            return options
        except: return []

    def fetch_portal_data(self, url) -> str:
        try:
            res = self.session.get(url, headers=self.headers, timeout=10)
            res.encoding = "shift_jis"
            soup = BeautifulSoup(res.text, "html.parser")
            for br in soup.find_all("br"): br.replace_with("\n")
            return soup.get_text().replace("\xa0", " ")
        except: return ""

# --- 改良版解析ロジック ---
def parse_schedule_text(year: int, text: str) -> List[Dict]:
    events = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    date_re = re.compile(r"(\d{1,2})/(\d{1,2})\([^)]+\)")
    time_re = re.compile(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")
    # 勤務時間のカッコ内を正確に抽出 (8h30), (9h), (0h45m) 等に対応
    work_h_re = re.compile(r"\((\d+)h(\d*)m?\)")

    ignore_words = [
        "メインメニュー", "ログアウト", "Ver 4.", "シフト確認", "所属別確定",
        "端末閲覧不可", "ﾊﾟｽﾜｰﾄﾞ初期設定", "【例】", "全ての日を表示する", "所属別"
    ]

    i = 0
    while i < len(lines):
        line = lines[i]
        date_m = date_re.search(line)
        
        if date_m:
            month, day = int(date_m.group(1)), int(date_m.group(2))
            start_t, end_t, work_h, pos, memo = None, None, 0.0, "", ""
            
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                if date_re.search(next_line): break # 次の日付が来たら終了

                tm = time_re.search(next_line)
                if tm:
                    if "[休" in next_line:
                        memo += next_line + " "
                    else:
                        # 勤務時間のメイン行
                        start_t, end_t = tm.groups()
                        h_match = work_h_re.search(next_line)
                        if h_match:
                            h = int(h_match.group(1))
                            m_str = h_match.group(2)
                            m = int(m_str) if m_str and m_str.isdigit() else 0
                            work_h = h + (m / 60.0)
                elif not any(word in next_line for word in ignore_words):
                    if next_line and not next_line.startswith("※") and not next_line.startswith("<"):
                        # 内容（DKM L等）を取得。既に取得済みの場合は上書きしない（後のゴミデータを拾わないため）
                        if not pos: pos = next_line
                j += 1
            
            if start_t:
                try:
                    s_dt = datetime(year, month, day, *map(int, start_t.split(":")))
                    e_dt = datetime(year, month, day, *map(int, end_t.split(":")))
                    if e_dt <= s_dt: e_dt += timedelta(days=1)

                    events.append({
                        "subject": pos if pos else "勤務",
                        "start_date_iso": s_dt.strftime("%Y-%m-%d"),
                        "start_time": start_t, "end_time": end_t,
                        "start_iso": s_dt.isoformat(), "end_iso": e_dt.isoformat(),
                        "work_hours": round(work_h, 2), "description": memo.strip(),
                    })
                except: pass
            i = j - 1
        i += 1
    return events

def calculate_night_hours(start_iso, end_iso) -> float:
    s, e = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
    night_h, curr = 0.0, s
    while curr < e:
        if curr.hour >= 22 or curr.hour < 5: night_h += 0.25
        curr += timedelta(minutes=15)
    return night_h

def is_japanese_holiday(date_str, creds):
    if not creds: return False
    try:
        service = build("calendar", "v3", credentials=creds)
        t_min, t_max = date_str + "T00:00:00Z", date_str + "T23:59:59Z"
        events = service.events().list(calendarId="ja.japanese#holiday@group.v.calendar.google.com", timeMin=t_min, timeMax=t_max).execute()
        return len(events.get("items", [])) > 0
    except: return False

# --- カレンダー表示 ---
class CalendarDisplay:
    def __init__(self, parent_frame, work_data, settings):
        self.parent, self.work_data, self.settings = parent_frame, work_data, settings
        self.render()

    def check_overwork(self) -> Set[date_cls]:
        limit = self.settings.get("max_week_days", 6)
        sorted_dates = sorted(self.work_data.keys())
        overwork_dates = set()
        streak, prev_date = 0, None
        for d in sorted_dates:
            streak = streak + 1 if prev_date and (d - prev_date).days == 1 else 1
            if streak > limit: overwork_dates.add(d)
            prev_date = d
        return overwork_dates

    def render(self):
        for w in self.parent.winfo_children(): w.destroy()
        if not self.work_data:
            tk.Label(self.parent, text="表示するシフトがありません", bg="#FFFFFF").pack(pady=20)
            return

        overwork_dates = self.check_overwork()
        dates = sorted(self.work_data.keys())
        curr = dates[0].replace(day=1)
        end_month = dates[-1].replace(day=1)

        while curr <= end_month:
            month_f = tk.Frame(self.parent, bg="#FFFFFF", pady=10)
            month_f.pack(fill="x", padx=10)
            tk.Label(month_f, text=f"{curr.year}年 {curr.month}月", font=("Segoe UI", 12, "bold"), fg=MD_COLORS["primary"], bg="#FFFFFF").pack(anchor="w", padx=5)
            
            header = tk.Frame(month_f, bg="#FFFFFF")
            header.pack(fill="x", pady=(5, 2))
            for d in ["月", "火", "水", "木", "金", "土", "日"]:
                f = tk.Frame(header, width=38, height=22, bg="#FFFFFF")
                f.pack_propagate(False); f.pack(side="left", padx=1)
                tk.Label(f, text=d, bg="#FFFFFF", fg="#999999", font=("Segoe UI", 8, "bold")).place(relx=0.5, rely=0.5, anchor="center")

            for week in calendar.monthcalendar(curr.year, curr.month):
                wf = tk.Frame(month_f, bg="#FFFFFF"); wf.pack(fill="x")
                for day in week:
                    is_empty = (day == 0)
                    bg_color, fg_color, border_color = "#FFFFFF", "#1C1B1F", "#EEEEEE"
                    if not is_empty:
                        d_obj = date_cls(curr.year, curr.month, day)
                        if d_obj in overwork_dates:
                            bg_color, fg_color = MD_COLORS["limitFill"], MD_COLORS["error"]
                        elif d_obj in self.work_data: bg_color = MD_COLORS["workdayFill"]
                    else: border_color = "#FFFFFF"

                    cell = tk.Frame(wf, width=38, height=32, bg=bg_color, highlightthickness=1, highlightbackground=border_color)
                    cell.pack_propagate(False); cell.pack(side="left", padx=1, pady=1)
                    if not is_empty:
                        tk.Label(cell, text=str(day), bg=bg_color, fg=fg_color, font=("Segoe UI", 10, "bold" if d_obj in self.work_data else "normal")).place(relx=0.5, rely=0.5, anchor="center")
            
            if curr.month == 12: curr = curr.replace(year=curr.year + 1, month=1)
            else: curr = curr.replace(month=curr.month + 1)
        
        self.parent.update_idletasks()
        if hasattr(self.parent.master, 'configure'): self.parent.master.configure(scrollregion=self.parent.master.bbox("all"))

# --- 設定・メインGUI ---
class CalendarSettingsTab(ttk.Frame):
    def __init__(self, parent, main_app):
        super().__init__(parent)
        self.main_app = main_app
        self.settings = {"max_week_days": 6, "max_week_days_option": "6days", "hourly_wage": 1290}
        self.load_settings(); self.setup_ui()

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f: self.settings.update(json.load(f))
            except: pass

    def setup_ui(self):
        main = ttk.Frame(self); main.pack(fill="both", expand=True, padx=12, pady=12)
        left = ttk.Frame(main); left.pack(side="left", fill="y", padx=(0, 12))

        wf = ttk.LabelFrame(left, text="連続勤務制限設定"); wf.pack(fill="x", pady=(0, 10))
        self.opt_var = tk.StringVar(value=self.settings["max_week_days_option"])
        ttk.Radiobutton(wf, text="最大7日", variable=self.opt_var, value="7days").pack(anchor="w", padx=5)
        ttk.Radiobutton(wf, text="最大6日", variable=self.opt_var, value="6days").pack(anchor="w", padx=5)

        pay_f = ttk.LabelFrame(left, text="時給設定"); pay_f.pack(fill="x", pady=5)
        self.ent_wage = ttk.Entry(pay_f); self.ent_wage.insert(0, str(self.settings["hourly_wage"])); self.ent_wage.pack(fill="x", padx=5, pady=5)
        ttk.Button(left, text="保存", command=self.save_settings, bootstyle="primary").pack(pady=10, fill="x")

        self.res_f = ttk.LabelFrame(left, text="概算"); self.res_f.pack(fill="x", pady=10)
        self.lbl_total_h = ttk.Label(self.res_f, text="総労働: 0.0 h"); self.lbl_total_h.pack(padx=5)
        self.lbl_total_pay = ttk.Label(self.res_f, text="¥ 0", font=("Segoe UI", 12, "bold")); self.lbl_total_pay.pack(padx=5, pady=5)

        self.calendar_frame = ttk.LabelFrame(main, text="勤務視覚化カレンダー"); self.calendar_frame.pack(side="right", fill="both", expand=True)
        self.canvas = tk.Canvas(self.calendar_frame, bg="#FFFFFF", highlightthickness=0); self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(self.calendar_frame, orient="vertical", command=self.canvas.yview); self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollable_frame = tk.Frame(self.canvas, bg="#FFFFFF")
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

    def save_settings(self):
        try:
            self.settings["max_week_days"] = 7 if self.opt_var.get() == "7days" else 6
            self.settings["max_week_days_option"] = self.opt_var.get()
            self.settings["hourly_wage"] = int(self.ent_wage.get())
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f: json.dump(self.settings, f)
            self.main_app.refresh_calendar()
        except: pass

    def update_payment_display(self, events):
        creds = authenticate_google(); wage = self.settings.get("hourly_wage", 1290)
        total_pay, total_h = 0.0, 0.0
        for e in events:
            h = e["work_hours"]; total_h += h
            rate = 1.35 if is_japanese_holiday(e["start_date_iso"], creds) else 1.00
            total_pay += (h * wage * rate) + (calculate_night_hours(e["start_iso"], e["end_iso"]) * wage * 0.25)
        self.lbl_total_h.config(text=f"総労働: {total_h:.2f}h"); self.lbl_total_pay.config(text=f"¥ {int(total_pay):,}")

class ShiftConverterGUI:
    def __init__(self, root):
        self.root = root; self.root.title(f"{__app_name__} v{__version__}"); self.root.geometry("1000x850")
        self.scraper = PortalScraper(); self.parsed_events = []; self.work_data = {}; self.setup_ui()

    def setup_ui(self):
        self.tabs = ttk.Notebook(self.root)
        self.tab_main, self.tab_config = ttk.Frame(self.tabs), CalendarSettingsTab(self.tabs, self)
        self.tabs.add(self.tab_main, text="取得・同期"); self.tabs.add(self.tab_config, text="分析・カレンダー")
        self.tabs.pack(expand=1, fill="both", padx=10, pady=10)

        mid = ttk.Frame(self.tab_main); mid.pack(fill="x", padx=10, pady=5)
        ttk.Button(mid, text="ポータルから取得", command=self.on_auto_fetch, bootstyle="secondary").pack(side="left", padx=5)
        self.entry_year = ttk.Entry(mid, width=8); self.entry_year.insert(0, str(datetime.now().year))
        self.entry_year.pack(side="right"); ttk.Label(mid, text="西暦:").pack(side="right", padx=5)

        self.text_area = scrolledtext.ScrolledText(self.tab_main, height=10, font=("Consolas", 10)); self.text_area.pack(fill="x", padx=10, pady=5)
        self.tree = ttk.Treeview(self.tab_main, columns=("d", "t", "s", "w"), show="headings", height=12)
        for c, h in zip(("d", "t", "s", "w"), ("日付", "時間", "内容", "実働")): self.tree.heading(c, text=h)
        self.tree.column("d", width=100); self.tree.column("t", width=150); self.tree.column("s", width=200); self.tree.pack(fill="both", expand=True, padx=10)

        bot = ttk.Frame(self.tab_main); bot.pack(fill="x", pady=10)
        ttk.Button(bot, text="解析実行", command=self.on_parse, bootstyle="primary").pack(side="left", padx=15)
        ttk.Button(bot, text="Googleカレンダー同期", command=self.on_gcal, bootstyle="success").pack(side="right", padx=15)

    def on_auto_fetch(self):
        uid = simpledialog.askstring("Login", "IDを入力"); pwd = simpledialog.askstring("Login", "Password", show="*")
        if uid and pwd and self.scraper.login(uid, pwd):
            options = self.scraper.get_month_options()
            sel_win = tk.Toplevel(self.root); lb = tk.Listbox(sel_win, width=40); lb.pack(padx=20, pady=20)
            for opt in options: lb.insert(tk.END, opt["label"])
            def confirm():
                idx = lb.curselection()
                if idx: self.text_area.delete("1.0", tk.END); self.text_area.insert("1.0", self.scraper.fetch_portal_data(options[idx[0]]["url"])); sel_win.destroy(); self.on_parse()
            ttk.Button(sel_win, text="解析開始", command=confirm).pack(pady=10)

    def on_parse(self):
        try:
            self.parsed_events = parse_schedule_text(int(self.entry_year.get()), self.text_area.get("1.0", tk.END))
            for item in self.tree.get_children(): self.tree.delete(item)
            self.work_data = {}
            for e in self.parsed_events:
                self.tree.insert("", tk.END, values=(e['start_date_iso'], f"{e['start_time']}-{e['end_time']}", e["subject"], f"{e['work_hours']}h"))
                dt = datetime.strptime(e["start_date_iso"], "%Y-%m-%d").date()
                self.work_data[dt] = self.work_data.get(dt, 0.0) + e["work_hours"]
            self.refresh_calendar()
        except Exception as e: messagebox.showerror("Error", str(e))

    def refresh_calendar(self):
        CalendarDisplay(self.tab_config.scrollable_frame, self.work_data, self.tab_config.settings)
        if self.parsed_events: self.tab_config.update_payment_display(self.parsed_events)

    def on_gcal(self):
        if not self.parsed_events: return
        creds = authenticate_google()
        if creds:
            service = build("calendar", "v3", credentials=creds)
            for e in self.parsed_events:
                body = {"summary": e["subject"], "description": e.get("description", ""), "start": {"dateTime": e["start_iso"], "timeZone": "Asia/Tokyo"}, "end": {"dateTime": e["end_iso"], "timeZone": "Asia/Tokyo"}}
                service.events().insert(calendarId="primary", body=body).execute()
            messagebox.showinfo("完了", "同期成功")

def authenticate_google():
    creds = None
    if os.path.exists(TOKEN_FILE): creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token: token.write(creds.to_json())
    return creds

if __name__ == "__main__":
    root = ttkb.Window(themename="flatly") if HAS_TTKB else tk.Tk()
    app = ShiftConverterGUI(root); root.mainloop()