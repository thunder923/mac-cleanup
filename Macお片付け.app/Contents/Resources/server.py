#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Macお片付け - Mac容量整理ツール (stdlib only)
起動するとローカルサーバーが立ち、ブラウザに整理UIが開く。
- ホーム配下の大容量ファイルを走査して分類（安全 / 重複 / 要判断）
- フォルダごとの合計容量ランキング（再帰集計）
- チェックしたファイルをゴミ箱へ移動（Finder経由 = 復元可能）
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = Path.home()
IS_MAC = sys.platform == "darwin"
DEFAULT_PORT = 17890
MIN_SIZE_DEFAULT = 50 * 1024 * 1024  # 50MB

# フォルダの「置き場所」優先度（重複時に残す方を決める: 小さい方が優先）
LOCATION_PRIORITY = {
    "Documents": 0, "Movies": 1, "Music": 1, "Pictures": 1,
    "Desktop": 2, "Downloads": 3, "": 4, "Library": 5,
}

# ライブラリ配下で「絶対に消しちゃダメ」な場所（候補に出さない・走査もしない）
# キーチェーン=パスワード集約、Mail/Messages=実データ、Photos/Calendars/Contacts/Accounts/
# Safari/Preferences/HomeKit 等=個人データと設定そのもの
LIBRARY_DENY = {
    "Keychains", "Preferences", "Mail", "Messages", "Photos",
    "Calendars", "Contacts", "Accounts", "Safari", "Cookies",
    "IdentityServices", "Passes", "HomeKit",
}

# ライブラリ配下で「消しても再生成される」扱いの場所
LIBRARY_SAFE_AREAS = {"Caches", "Logs", "HTTPStorages", "Saved Application State"}

SCAN = {
    "status": "idle",        # idle | scanning | done | error
    "phase": "",
    "files": [],             # 分類済みファイル一覧
    "folders": [],           # フォルダ合計ランキング
    "disk": {},
    "scanned_count": 0,
    "started_at": None,
    "finished_at": None,
    "error": "",
}
SCAN_LOCK = threading.Lock()


def fmt_date(ts):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except Exception:
        return ""


def disk_info():
    try:
        u = shutil.disk_usage("/")
        return {"total": u.total, "used": u.used, "free": u.free}
    except Exception:
        return {}


def list_installed_apps():
    apps = set()
    if IS_MAC:
        for base in (Path("/Applications"), HOME / "Applications"):
            try:
                for p in base.iterdir():
                    apps.add(p.name.lower().replace(".app", ""))
            except Exception:
                pass
    return apps


def dmg_probably_installed(path: Path, apps):
    stem = path.stem.lower()
    # "github-copilot-darwin-arm64 (1)" -> "github", "copilot" のような語で照合
    cleaned = stem.replace("-", " ").replace("_", " ")
    for ch in "()[]0123456789.":
        cleaned = cleaned.replace(ch, " ")
    tokens = [t for t in cleaned.split() if len(t) >= 4 and t not in
              ("darwin", "arm64", "macos", "apple", "silicon", "setup", "installer")]
    for t in tokens:
        for a in apps:
            if t in a.replace(" ", "") or t in a:
                return True
    return False


def quick_hash(path: Path, size: int):
    """先頭・中間・末尾の部分ハッシュ（重複の目安判定用）"""
    h = hashlib.sha256()
    chunk = 256 * 1024
    try:
        with open(path, "rb") as f:
            h.update(f.read(chunk))
            if size > chunk * 3:
                f.seek(size // 2)
                h.update(f.read(chunk))
                f.seek(max(0, size - chunk))
                h.update(f.read(chunk))
        h.update(str(size).encode())
        return h.hexdigest()
    except Exception:
        return ""


def top_folder_of(path: Path):
    try:
        rel = path.relative_to(HOME)
        return rel.parts[0] if len(rel.parts) > 1 else ""
    except Exception:
        return ""


def classify_file(path: Path, size: int, mtime: float, apps):
    ext = path.suffix.lower()
    name = path.name.lower()
    top = top_folder_of(path)
    item = {
        "path": str(path), "name": path.name, "size": size,
        "modified": fmt_date(mtime), "folder": top or "ホーム直下",
        "area": top or "ホーム直下",
        "category": "large", "badge": "要判断", "note": "",
    }
    if top == "Library":
        # ライブラリ配下: エリア（Library直下のフォルダ名）で性格が決まる
        try:
            rel = path.relative_to(HOME / "Library")
            area = rel.parts[0] if rel.parts else ""
        except Exception:
            area = ""
        item["area"] = "Library/" + area if area else "Library"
        s = str(path)
        if area in LIBRARY_SAFE_AREAS:
            item.update(category="safe", badge="安全",
                        note="ライブラリの%s。再生成されるものなので消してOK" % area)
            return item
        if area == "Developer" and ("DerivedData" in s or "CoreSimulator" in s):
            item.update(category="safe", badge="安全",
                        note="Xcodeの生成物・シミュレータ。再生成できるので消してOK")
            return item
        item["category"] = "library"
        item["badge"] = "要注意"
        if "MobileSync" in s:
            item["note"] = "iPhone/iPadのバックアップ。古い端末のものは消してOKなことが多い"
        elif area == "Containers":
            item["note"] = "アプリのデータ本体（Docker・SNSアプリ等）。消すとそのアプリのデータが消える"
        elif area == "Group Containers":
            item["note"] = "アプリ共有のデータ本体。消すとアプリのデータが消えることがある"
        elif area == "Application Support":
            item["note"] = "アプリの設定・データ。消すとそのアプリの状態がリセットされることがある"
        elif area == "Android":
            item["note"] = "Android SDK。開発で使っていなければ消してOK"
        elif area == "Developer":
            item["note"] = "開発関連データ。何か確認してから"
        else:
            item["note"] = "ライブラリ内のデータ（%s）。内容を確認してから" % area
        return item
    if name.endswith((".part", ".crdownload", ".download", ".opdownload")):
        item.update(category="safe", badge="安全",
                    note="未完了ダウンロードの残骸")
        return item
    if ext in (".dmg", ".pkg"):
        if dmg_probably_installed(path, apps):
            item.update(category="safe", badge="安全",
                        note="インストーラー（インストール済みの可能性が高い）")
        else:
            item.update(category="safe", badge="安全",
                        note="インストーラー。対応アプリを入れた後なら不要")
        return item
    if ext in (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"):
        item["note"] = "圧縮ファイル。展開済みなら不要"
        return item
    if top == "" :
        item["note"] = "ホーム直下に置かれたファイル。置き忘れが多い場所"
        return item
    return item


def scan_files(min_size, apps):
    targets = []
    for label in ("Desktop", "Downloads", "Documents", "Movies", "Music", "Pictures"):
        p = HOME / label
        if p.is_dir():
            targets.append(p)
    found = []
    scanned = 0
    # 主要フォルダを再帰走査
    for root_dir in targets:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except Exception:
                    continue
                if st.st_size >= min_size:
                    found.append(classify_file(fp, st.st_size, st.st_mtime, apps))
    # ホーム直下（非再帰）
    try:
        for p in HOME.iterdir():
            if p.is_file():
                scanned += 1
                try:
                    st = p.stat()
                except Exception:
                    continue
                if st.st_size >= min_size:
                    found.append(classify_file(p, st.st_size, st.st_mtime, apps))
    except Exception:
        pass
    # ライブラリ（「絶対ダメ」な場所は丸ごと除外して再帰走査）
    lib = HOME / "Library"
    if lib.is_dir():
        for dirpath, dirnames, filenames in os.walk(lib):
            if Path(dirpath) == lib:
                dirnames[:] = [d for d in dirnames if d not in LIBRARY_DENY]
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except Exception:
                    continue
                if st.st_size >= min_size:
                    found.append(classify_file(fp, st.st_size, st.st_mtime, apps))
    with SCAN_LOCK:
        SCAN["scanned_count"] = scanned
    return found


def mark_duplicates(files):
    by_size = {}
    for f in files:
        if f["size"] >= 10 * 1024 * 1024:
            by_size.setdefault(f["size"], []).append(f)
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_hash = {}
        for f in group:
            h = quick_hash(Path(f["path"]), size)
            if h:
                by_hash.setdefault(h, []).append(f)
        for h, dups in by_hash.items():
            if len(dups) < 2:
                continue
            # 残す方: 置き場所の優先度 → 新しい方
            def keep_key(f):
                return (LOCATION_PRIORITY.get(f["folder"], 4), )
            keeper = sorted(dups, key=keep_key)[0]
            for f in dups:
                if f is keeper:
                    f["note"] = (f["note"] + " " if f["note"] else "") + \
                        "重複あり（こちらが残す方）"
                else:
                    if f["category"] == "large":
                        f["category"] = "dup"
                        f["badge"] = "重複"
                    f["note"] = "同内容が「%s」にもあり（そちらを残す想定）" % keeper["path"]
    return files


def scan_folders():
    """ホーム直下の各フォルダを再帰集計してランキング化"""
    results = []
    entries = []
    try:
        entries = [p for p in HOME.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except Exception:
        entries = []
    for folder in entries:
        total = 0
        count = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".Trash"]
            for fn in filenames:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                    count += 1
                except Exception:
                    pass
        if total > 0:
            results.append({"path": str(folder), "name": folder.name,
                            "size": total, "files": count})
    # Library の中でも重くなりがちな場所は個別に
    lib_targets = ["Caches", "Application Support", "Containers",
                   "Group Containers", "Developer", "Logs"]
    for sub in lib_targets:
        folder = HOME / "Library" / sub
        if not folder.is_dir():
            continue
        total = 0
        count = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            for fn in filenames:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                    count += 1
                except Exception:
                    pass
        if total > 0:
            results.append({"path": str(folder), "name": "Library/" + sub,
                            "size": total, "files": count})
    results.sort(key=lambda x: -x["size"])
    return results[:20]


def run_scan(min_size=MIN_SIZE_DEFAULT):
    with SCAN_LOCK:
        if SCAN["status"] == "scanning":
            return
        SCAN.update(status="scanning", phase="ディスク情報を取得中",
                    files=[], folders=[], error="", scanned_count=0,
                    started_at=time.time(), finished_at=None)
    try:
        apps = list_installed_apps()
        with SCAN_LOCK:
            SCAN["disk"] = disk_info()
            SCAN["phase"] = "大容量ファイルを探しています（Desktop / Downloads / ライブラリ など）"
        files = scan_files(min_size, apps)
        with SCAN_LOCK:
            SCAN["phase"] = "重複をチェックしています"
        files = mark_duplicates(files)
        files.sort(key=lambda x: -x["size"])
        with SCAN_LOCK:
            SCAN["files"] = files
            SCAN["phase"] = "フォルダごとの合計を集計しています（少し時間がかかります）"
        folders = scan_folders()
        with SCAN_LOCK:
            SCAN["folders"] = folders
            SCAN["status"] = "done"
            SCAN["phase"] = "完了"
            SCAN["finished_at"] = time.time()
    except Exception as e:
        with SCAN_LOCK:
            SCAN["status"] = "error"
            SCAN["error"] = str(e)
            SCAN["phase"] = "エラー"


def trash_one(path_str):
    p = Path(path_str)
    # 安全弁: ホーム配下以外は触らない
    try:
        p.resolve().relative_to(HOME.resolve())
    except Exception:
        return {"path": path_str, "ok": False, "error": "ホーム配下ではないため対象外"}
    if not p.exists():
        return {"path": path_str, "ok": False, "error": "既に存在しない"}
    if IS_MAC:
        script = ('on run argv\n'
                  'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\n'
                  'end run')
        try:
            r = subprocess.run(["osascript", "-e", script, "--", str(p)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and not p.exists():
                return {"path": path_str, "ok": True, "method": "Finder"}
        except Exception:
            pass
    # フォールバック: ~/.Trash へ移動（同名は連番）
    trash_dir = HOME / ".Trash"
    trash_dir.mkdir(exist_ok=True)
    dest = trash_dir / p.name
    i = 2
    while dest.exists():
        dest = trash_dir / ("%s %d%s" % (p.stem, i, p.suffix))
        i += 1
    try:
        shutil.move(str(p), str(dest))
        return {"path": path_str, "ok": True, "method": "Trashフォルダへ移動"}
    except Exception as e:
        return {"path": path_str, "ok": False, "error": str(e)}


def reveal_in_finder(path_str):
    if IS_MAC and Path(path_str).exists():
        subprocess.run(["open", "-R", path_str], capture_output=True, timeout=15)
        return True
    return False


HTML = r"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Macお片付け</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2330;--muted:#68707d;--border:#dde1e8;--accent:#2563eb;--safe:#15803d;--dup:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1e232c;--text:#eef1f6;--muted:#9aa3b2;--border:#333a46;--accent:#5b93ff;--safe:#4ade80;--dup:#fbbf24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Hiragino Sans",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 110px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;line-height:1.6}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 16px;margin-top:14px}
.bar{height:14px;border-radius:999px;background:var(--border);overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#f59e0b,#ef4444)}
.nums{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}.nums b{font-size:17px}
h2{font-size:15.5px;margin:0 0 4px}.d{color:var(--muted);font-size:12.5px;margin:0 0 8px;line-height:1.55}
.row{display:flex;gap:10px;align-items:flex-start;padding:9px 10px;border:1px solid var(--border);border-radius:10px;margin-bottom:6px;background:var(--card);cursor:pointer}
.row input{margin-top:3px;width:17px;height:17px;accent-color:var(--accent);flex:none}
.row .main{flex:1;min-width:0}.name{font-size:13.5px;font-weight:600;word-break:break-all;line-height:1.45}
.meta{font-size:11.5px;color:var(--muted);margin-top:2px;line-height:1.5;word-break:break-all}
.size{font-size:13.5px;font-weight:700;white-space:nowrap}
.badge{display:inline-block;font-size:10.5px;font-weight:700;border-radius:999px;padding:1px 8px;margin-left:6px;border:1px solid var(--border);vertical-align:1px}
.b-safe{color:var(--safe)}.b-dup{color:var(--dup)}.b-judge{color:var(--muted)}
.folder{display:flex;justify-content:space-between;padding:6px 2px;border-bottom:1px dashed var(--border);font-size:13px}
.folder:last-child{border-bottom:none}.folder .fp{color:var(--muted);font-size:11.5px}
.btn{border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:999px;padding:8px 14px;font-size:13px;cursor:pointer}
.btn.primary{background:var(--accent);color:#fff;border-color:transparent;font-weight:700}
.btn.danger{background:#dc2626;color:#fff;border-color:transparent;font-weight:700}
.btn:disabled{opacity:.45;cursor:default}
.sticky{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--border);padding:12px 16px;display:flex;gap:12px;align-items:center;justify-content:center;flex-wrap:wrap;box-shadow:0 -4px 18px rgba(0,0,0,.08)}
.sticky b{font-size:20px;color:var(--accent)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;align-items:center;justify-content:center;padding:16px;z-index:9}
.modal.open{display:flex}.mbox{background:var(--card);border-radius:16px;max-width:560px;width:100%;max-height:80vh;overflow:auto;padding:18px}
.mlist{font-size:12.5px;line-height:1.7;word-break:break-all;border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin:10px 0;max-height:38vh;overflow:auto}
.reveal{color:var(--accent);font-size:11.5px;cursor:pointer;text-decoration:underline}
select{font-size:13px;padding:6px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--text)}
</style></head><body>
<div class="wrap">
 <h1>Macお片付け <span style="font-size:12px;color:var(--muted)">v1.1</span></h1>
 <div class="sub">このMacの中の大容量ファイルを探して、安全なもの・重複・要判断に分けて表示します。チェックして「ゴミ箱へ入れる」を押すと、Finderのゴミ箱へ移動します（あとから戻せます）。ライブラリも対象ですが、キーチェーン（パスワード）・メール・メッセージ・写真・カレンダー・連絡先・アカウント・Safari・設定ファイルなど「絶対に消しちゃダメ」な場所は最初から表示しません。</div>
 <div class="card"><h2>ディスク使用量</h2><div class="bar"><i id="diskBar" style="width:0%"></i></div>
 <div class="nums"><span>合計 <b id="dTotal">-</b></span><span>使用中 <b id="dUsed">-</b></span><span>空き <b id="dFree">-</b></span></div></div>
 <div class="card"><h2>フォルダ別ランキング（再帰集計）</h2><p class="d">ホーム直下のフォルダと、重くなりがちな Library 配下の合計です。</p><div id="folders">集計中…</div></div>
 <div class="card">
  <h2>整理コントロール</h2>
  <p class="d" id="status">スキャン中…</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
   <button class="btn primary" id="btnSafe" type="button">安全＋重複だけ全選択</button>
   <button class="btn" id="btnAll" type="button">全選択</button>
   <button class="btn" id="btnClear" type="button">全解除</button>
   <label style="font-size:12.5px;color:var(--muted)">最小サイズ
    <select id="minSel"><option value="10">10MB</option><option value="50" selected>50MB</option><option value="100">100MB</option><option value="500">500MB</option><option value="1024">1GB</option></select></label>
   <button class="btn" id="btnRescan" type="button">再スキャン</button>
  </div>
  <p class="d" style="margin-top:12px">場所で絞り込み:</p>
  <div id="folderChips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
 </div>
 <div id="sections"></div>
 <div class="card"><h2>使い方・プライバシーポリシー</h2>
 <p class="d">使い方：スキャンが終わるのを待って、消したいファイルにチェック → 下の「ゴミ箱へ入れる」→ 確認画面で対象を確認して実行します。削除ではなく Finder のゴミ箱への移動だけなので、あとから戻せます。詳しい使い方は同梱の「使い方.txt」を見てください。</p>
 <p class="d">プライバシー：このツールの通信はこの Mac の中（127.0.0.1）だけです。ファイル名・ファイルの中身・スキャン結果などの情報が外部のサーバーへ送信されることは一切ありません。動作ログもこの Mac 内（~/Library/Logs/MacCleanup.log）にのみ保存されます。</p></div>
</div>
<div class="sticky"><span>選択中 <b id="selTotal">0 GB</b>（<span id="selCount">0</span>件）</span>
<button class="btn danger" id="btnTrash" type="button" disabled>ゴミ箱へ入れる</button></div>
<div class="modal" id="modal"><div class="mbox">
 <h2 style="margin-top:0">ゴミ箱へ入れますか？</h2>
 <p class="d" id="mSummary"></p><div class="mlist" id="mList"></div>
 <p class="d">移動先はゴミ箱です。完全削除はしません。Finderのゴミ箱から戻せます。</p>
 <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn" id="mCancel" type="button">やめる</button><button class="btn danger" id="mOk" type="button">ゴミ箱へ入れる</button></div>
</div></div>
<script>
var FILES=[],FOLDERS=[],DISK={},STATUS="idle",checked={};
function gb(n){if(n>=1e9)return (n/1e9).toFixed(2)+" GB";if(n>=1e6)return (n/1e6).toFixed(0)+" MB";return n+" B"}
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")}
function byId(id){return document.getElementById(id)}
function renderDisk(){if(!DISK.total)return;byId("dTotal").textContent=gb(DISK.total);byId("dUsed").textContent=gb(DISK.used);byId("dFree").textContent=gb(DISK.free);byId("diskBar").style.width=Math.round(DISK.used/DISK.total*100)+"%"}
function renderFolders(){var el=byId("folders");if(!FOLDERS.length){el.textContent="集計中…";return}el.innerHTML="";FOLDERS.forEach(function(f){var d=document.createElement("div");d.className="folder";d.innerHTML="<div>"+esc(f.name)+"<div class='fp'>"+esc(f.path)+"／"+f.files.toLocaleString()+"ファイル</div></div><b>"+gb(f.size)+"</b>";el.appendChild(d)})}
var SECS=[["safe","安全に消せるもの","インストーラーと未完了ファイル、ライブラリのキャッシュ等。対応アプリを入れた後なら不要です。"],["dup","重複してるもの（片方は残ります）","同じ内容が複数あるもの。表示側だけ消して、もう片方は残ります。"],["library","ライブラリの中身（要注意）","絶対に消せない場所（キーチェーン・メール等）は最初から出していません。ここはアプリのデータ本体が多いので、内容を確認してから消してください。"],["large","大容量ファイル（要判断）","サイズ順。大事な素材かもしれないので、1つずつ確認してください。"]];
var FILTER="すべて";
function visibleFiles(){return FILTER==="すべて"?FILES:FILES.filter(function(f){return f.folder===FILTER})}
function renderChips(){var el=byId("folderChips");if(!el)return;el.innerHTML="";var totals={};FILES.forEach(function(f){totals[f.folder]=(totals[f.folder]||0)+f.size});
var names=["すべて"].concat(Object.keys(totals).sort(function(a,b){return totals[b]-totals[a]}));
names.forEach(function(n){var b=document.createElement("button");b.type="button";b.className="btn"+(FILTER===n?" primary":"");b.textContent=n==="すべて"?"すべて（"+gb(FILES.reduce(function(a,f){return a+f.size},0))+"）":n+"（"+gb(totals[n])+"）";b.addEventListener("click",function(){FILTER=n;renderChips();renderFiles()});el.appendChild(b)})}
function renderFiles(){var box=byId("sections");box.innerHTML="";var VIS=visibleFiles();renderChips();SECS.forEach(function(sec){var items=VIS.filter(function(f){return f.category===sec[0]});if(!items.length)return;var card=document.createElement("div");card.className="card";card.innerHTML="<h2>"+sec[1]+"（"+items.length+"件・"+gb(items.reduce(function(a,f){return a+f.size},0))+"）</h2><p class='d'>"+sec[2]+"</p>";
items.forEach(function(f){var row=document.createElement("label");row.className="row";var cb=document.createElement("input");cb.type="checkbox";cb.checked=!!checked[f.path];cb.addEventListener("change",function(){if(cb.checked)checked[f.path]=true;else delete checked[f.path];renderSel()});
var main=document.createElement("div");main.className="main";var nm=document.createElement("div");nm.className="name";nm.textContent=f.name;var bd=document.createElement("span");bd.className="badge "+(f.badge==="安全"?"b-safe":f.badge==="重複"?"b-dup":"b-judge");bd.textContent=f.badge;nm.appendChild(bd);
var mt=document.createElement("div");mt.className="meta";mt.textContent=f.path+"／"+f.modified+(f.note?"／"+f.note:"");
var rv=document.createElement("div");var rl=document.createElement("span");rl.className="reveal";rl.textContent="Finderで表示";rl.addEventListener("click",function(e){e.preventDefault();fetch("api/reveal",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:f.path})})});rv.appendChild(rl);
main.appendChild(nm);main.appendChild(mt);main.appendChild(rv);
var sz=document.createElement("div");sz.className="size";sz.textContent=gb(f.size);row.appendChild(cb);row.appendChild(main);row.appendChild(sz);card.appendChild(row)});
box.appendChild(card)});renderSel()}
function selItems(){return FILES.filter(function(f){return checked[f.path]})}
function renderSel(){var it=selItems();byId("selTotal").textContent=gb(it.reduce(function(a,f){return a+f.size},0));byId("selCount").textContent=it.length;byId("btnTrash").disabled=!it.length}
function poll(){fetch("api/data").then(function(r){return r.json()}).then(function(d){STATUS=d.status;FILES=d.files||[];FOLDERS=d.folders||[];DISK=d.disk||{};
byId("status").textContent=(d.status==="done"?"スキャン完了。":"スキャン中："+(d.phase||""))+" 候補 "+FILES.length+"件・合計 "+gb(FILES.reduce(function(a,f){return a+f.size},0))+(d.scanned_count?"／走査 "+d.scanned_count.toLocaleString()+"ファイル":"")+(d.error?" エラー: "+d.error:"");
renderDisk();renderFolders();renderFiles();if(d.status==="scanning")setTimeout(poll,1500)}).catch(function(){setTimeout(poll,2000)})}
byId("btnSafe").addEventListener("click",function(){FILES.forEach(function(f){if(f.category==="safe"||f.category==="dup")checked[f.path]=true});renderFiles()});
byId("btnAll").addEventListener("click",function(){FILES.forEach(function(f){checked[f.path]=true});renderFiles()});
byId("btnClear").addEventListener("click",function(){checked={};renderFiles()});
byId("btnRescan").addEventListener("click",function(){var mb=byId("minSel").value;fetch("api/rescan?min_mb="+mb,{method:"POST"}).then(function(){checked={};poll()})});
byId("btnTrash").addEventListener("click",function(){var it=selItems();byId("mSummary").textContent=it.length+"件・合計 "+gb(it.reduce(function(a,f){return a+f.size},0))+" をゴミ箱へ入れます。対象は以下です。";byId("mList").innerHTML=it.map(function(f){return esc(f.path)+"（"+gb(f.size)+"）"}).join("<br>");byId("modal").classList.add("open")});
byId("mCancel").addEventListener("click",function(){byId("modal").classList.remove("open")});
byId("mOk").addEventListener("click",function(){var it=selItems();byId("mOk").disabled=true;byId("mOk").textContent="移動中…";
fetch("api/trash",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paths:it.map(function(f){return f.path})})}).then(function(r){return r.json()}).then(function(res){
byId("modal").classList.remove("open");byId("mOk").disabled=false;byId("mOk").textContent="ゴミ箱へ入れる";
var ok=res.results.filter(function(x){return x.ok}),ng=res.results.filter(function(x){return !x.ok});
var done={};ok.forEach(function(x){done[x.path]=true;delete checked[x.path]});FILES=FILES.filter(function(f){return !done[f.path]});
alert("ゴミ箱へ入れました："+ok.length+"件・"+gb(ok.reduce(function(a,x){var f=it.find(function(y){return y.path===x.path});return a+(f?f.size:0)},0))+(ng.length?"\n失敗："+ng.length+"件（"+ng.map(function(x){return x.error||x.path}).join("、")+"）":""));
renderFiles();renderDisk()})});
poll();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/data":
            with SCAN_LOCK:
                self._json({
                    "status": SCAN["status"], "phase": SCAN["phase"],
                    "files": SCAN["files"], "folders": SCAN["folders"],
                    "disk": disk_info() if SCAN["status"] == "done" else SCAN["disk"],
                    "scanned_count": SCAN["scanned_count"],
                    "error": SCAN["error"],
                })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        if u.path == "/api/rescan":
            qs = parse_qs(u.query)
            try:
                min_mb = int(qs.get("min_mb", ["50"])[0])
            except Exception:
                min_mb = 50
            with SCAN_LOCK:
                SCAN["status"] = "idle"
            threading.Thread(target=run_scan, args=(min_mb * 1024 * 1024,),
                             daemon=True).start()
            self._json({"ok": True})
        elif u.path == "/api/trash":
            paths = data.get("paths") or []
            if not isinstance(paths, list) or not paths:
                self._json({"ok": False, "error": "対象がありません"}, 400)
                return
            results = [trash_one(str(p)) for p in paths]
            self._json({"ok": True, "results": results})
        elif u.path == "/api/reveal":
            ok = reveal_in_finder(str(data.get("path") or ""))
            self._json({"ok": ok})
        else:
            self._json({"error": "not found"}, 404)


def main():
    port = DEFAULT_PORT
    server = None
    for cand in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError:
            continue
    if server is None:
        print("ポートを確保できませんでした")
        return 1
    threading.Thread(target=run_scan, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    print("Macお片付け を起動しました: %s" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
