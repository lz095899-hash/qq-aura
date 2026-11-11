"""SQLite 状态层：消息流水、长期记忆、心情、空间记录、主动搭话计数。

只做存取，不做判断。所有表都带 chat 作用域，跨会话的记忆在 SQL 层就取不到。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL, role TEXT NOT NULL,
  speaker_id TEXT, speaker_name TEXT, text TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL, about_name TEXT DEFAULT '', about_id TEXT DEFAULT '',
  scope_chat TEXT NOT NULL DEFAULT 'global',
  importance REAL DEFAULT 0.5,
  source TEXT DEFAULT 'tool',
  tags TEXT DEFAULT '',
  created_at REAL NOT NULL, last_recalled REAL DEFAULT 0, recall_count INTEGER DEFAULT 0,
  valid_from TEXT DEFAULT '', valid_until TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_chat, about_id);
CREATE TABLE IF NOT EXISTS mood (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  valence REAL DEFAULT 0.1, energy REAL DEFAULT 0.5,
  label TEXT DEFAULT '平静', note TEXT DEFAULT '', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qzone_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tid TEXT DEFAULT '',
  content TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
  key TEXT PRIMARY KEY, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_count (
  day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER DEFAULT 0,
  PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS consolidated (
  day TEXT PRIMARY KEY, done_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL, text TEXT NOT NULL, due_at REAL NOT NULL,
  repeat_rule TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  created_at REAL NOT NULL, fired_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(enabled, due_at);
CREATE TABLE IF NOT EXISTS couple_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, event_date TEXT NOT NULL,
  repeat_yearly INTEGER DEFAULT 1, note TEXT DEFAULT ''
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.fts = self._init_fts()

    def _init_fts(self):
        try:
            with self._lock:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                    "text, about_name, content='memories', content_rowid='id', tokenize='trigram')")
                self.db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN "
                    "INSERT INTO memories_fts(rowid, text, about_name) VALUES (new.id, new.text, new.about_name); END")
                self.db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, text, about_name) VALUES('delete', old.id, old.text, old.about_name); END")
                self.db.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, text, about_name) VALUES('delete', old.id, old.text, old.about_name); "
                    "INSERT INTO memories_fts(rowid, text, about_name) VALUES (new.id, new.text, new.about_name); END")
                self.db.commit()
            return True
        except sqlite3.OperationalError:
            return False

    # ---------- kv ----------
    def get(self, key, default=None):
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.db.commit()

    # ---------- 消息 ----------
    def add_message(self, chat_id, role, text, speaker_id="", speaker_name="", ts=None):
        with self._lock:
            self.db.execute("INSERT INTO messages(chat_id,role,speaker_id,speaker_name,text,created_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (chat_id, role, str(speaker_id or ""), speaker_name or "", text,
                             ts or time.time()))
            self.db.commit()

    def history(self, chat_id, limit=24):
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        return list(reversed(rows))

    def last_user_ts(self, chat_id):
        with self._lock:
            row = self.db.execute(
                "SELECT max(created_at) t FROM messages WHERE chat_id=? AND role='user'",
                (chat_id,)).fetchone()
        return row["t"] if row and row["t"] else 0.0

    def messages_between(self, chat_id, start_ts, end_ts):
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND created_at>=? AND created_at<? ORDER BY id",
                (chat_id, start_ts, end_ts)).fetchall()
        return rows

    # ---------- 记忆 ----------
    def add_memory(self, text, about_name="", about_id="", scope_chat="global",
                   importance=0.5, source="tool", tags=""):
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO memories(text,about_name,about_id,scope_chat,importance,source,tags,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (text, about_name or "", str(about_id or ""), scope_chat, float(importance),
                 source, tags or "", time.time()))
            self.db.commit()
            return cur.lastrowid

    def memories_for(self, scopes, limit=200):
        if not scopes:
            return []
        ph = ",".join("?" * len(scopes))
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM memories WHERE scope_chat IN ({ph}) ORDER BY importance DESC, created_at DESC LIMIT ?",
                (*scopes, limit)).fetchall()
        return rows

    def memory_by_id(self, mid):
        with self._lock:
            return self.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()

    def touch_memory(self, mid):
        with self._lock:
            self.db.execute("UPDATE memories SET last_recalled=?, recall_count=recall_count+1 WHERE id=?",
                            (time.time(), mid))
            self.db.commit()

    def delete_memory(self, mid):
        with self._lock:
            self.db.execute("DELETE FROM memories WHERE id=?", (mid,))
            self.db.commit()

    def memory_count(self):
        with self._lock:
            return self.db.execute("SELECT count(*) c FROM memories").fetchone()["c"]

    # ---------- 心情 ----------
    def get_mood(self):
        with self._lock:
            row = self.db.execute("SELECT * FROM mood WHERE id=1").fetchone()
        if not row:
            self.set_mood(0.1, 0.5, "平静")
            with self._lock:
                row = self.db.execute("SELECT * FROM mood WHERE id=1").fetchone()
        return row

    def set_mood(self, valence, energy, label="", note=""):
        valence = max(-1.0, min(1.0, float(valence)))
        energy = max(0.0, min(1.0, float(energy)))
        with self._lock:
            self.db.execute(
                "INSERT INTO mood(id,valence,energy,label,note,updated_at) VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET valence=excluded.valence, energy=excluded.energy, "
                "label=excluded.label, note=excluded.note, updated_at=excluded.updated_at",
                (valence, energy, label, note, time.time()))
            self.db.commit()

    # ---------- 空间 / 去重 / 日计数 ----------
    def add_qzone(self, tid, content):
        with self._lock:
            self.db.execute("INSERT INTO qzone_log(tid,content,created_at) VALUES(?,?,?)",
                            (str(tid or ""), content, time.time()))
            self.db.commit()

    def qzone_today_count(self):
        start = time.time() - 86400
        with self._lock:
            return self.db.execute("SELECT count(*) c FROM qzone_log WHERE created_at>=?",
                                   (start,)).fetchone()["c"]

    def seen(self, key):
        with self._lock:
            row = self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone()
        return bool(row)

    def mark_seen(self, key):
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO seen(key,created_at) VALUES(?,?)", (key, time.time()))
            self.db.commit()

    def bump(self, kind, day=None):
        day = day or time.strftime("%Y-%m-%d")
        with self._lock:
            self.db.execute("INSERT INTO daily_count(day,kind,n) VALUES(?,?,1) "
                            "ON CONFLICT(day,kind) DO UPDATE SET n=n+1", (day, kind))
            self.db.commit()

    def count_today(self, kind, day=None):
        day = day or time.strftime("%Y-%m-%d")
        with self._lock:
            row = self.db.execute("SELECT n FROM daily_count WHERE day=? AND kind=?", (day, kind)).fetchone()
        return row["n"] if row else 0

    def was_consolidated(self, day):
        with self._lock:
            return bool(self.db.execute("SELECT 1 FROM consolidated WHERE day=?", (day,)).fetchone())

    def mark_consolidated(self, day):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO consolidated(day,done_at) VALUES(?,?)",
                            (day, time.time()))
            self.db.commit()

    # ---------- 提醒 / 纪念日 ----------
    def add_reminder(self, chat_id, text, due_at, repeat_rule=""):
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO reminders(chat_id,text,due_at,repeat_rule,created_at) VALUES(?,?,?,?,?)",
                (chat_id, text, float(due_at), repeat_rule or "", time.time()))
            self.db.commit()
            return cur.lastrowid

    def due_reminders(self, now=None, limit=20):
        now = float(now or time.time())
        with self._lock:
            return self.db.execute(
                "SELECT * FROM reminders WHERE enabled=1 AND due_at<=? ORDER BY due_at LIMIT ?",
                (now, int(limit))).fetchall()

    def list_reminders(self, chat_id, limit=20):
        with self._lock:
            return self.db.execute(
                "SELECT * FROM reminders WHERE chat_id=? AND enabled=1 ORDER BY due_at LIMIT ?",
                (chat_id, int(limit))).fetchall()

    def fire_reminder(self, rid, next_due=0):
        with self._lock:
            if next_due:
                self.db.execute("UPDATE reminders SET due_at=?, fired_at=? WHERE id=?",
                                (float(next_due), time.time(), int(rid)))
            else:
                self.db.execute("UPDATE reminders SET enabled=0, fired_at=? WHERE id=?",
                                (time.time(), int(rid)))
            self.db.commit()

    def delete_reminder(self, rid, chat_id=""):
        with self._lock:
            if chat_id:
                cur = self.db.execute("UPDATE reminders SET enabled=0 WHERE id=? AND chat_id=?",
                                      (int(rid), chat_id))
            else:
                cur = self.db.execute("UPDATE reminders SET enabled=0 WHERE id=?", (int(rid),))
            self.db.commit()
            return cur.rowcount > 0

    def close(self):
        with self._lock:
            self.db.close()