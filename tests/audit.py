# -*- coding: utf-8 -*-
import importlib.util, io, json, os, tempfile, contextlib, re, sys, datetime
SRC = os.environ.get("PLUGIN_SRC") or os.path.join(os.path.dirname(__file__), "..", "opencode-usage.5m.py")
os.environ.setdefault("SWIFTBAR_PLUGIN_PATH", SRC)
FAILS = []
def T(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond: FAILS.append(name)

def load():
    spec = importlib.util.spec_from_file_location("p", SRC)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.ensure_pil = lambda: False
    tmp = tempfile.mkdtemp()
    m.CONFIG_DIR = tmp; m.CONFIG_PATH = os.path.join(tmp, "config.json")
    m.DATA_DIR = tmp; m.CACHE_PATH = os.path.join(tmp, "cache.json")
    m.NOTIFY_PATH = os.path.join(tmp, "notify.json")
    m.refresh_self = lambda: None
    return m

def grab(m, fn):
    b = io.StringIO()
    with contextlib.redirect_stdout(b): fn()
    return b.getvalue()

print("== 1 配置迁移与校验 ==")
m = load()
m.save_config({"apiKey": "oc_sk_" + "a"*20})                      # v1 单 key 旧格式
cfg = m.load_config()
T("v1 apiKey 迁移为 keys", len(cfg["keys"])==1 and cfg["keys"][0]["key"].endswith("a"*20))
m.save_config({"lang":"zh","active":9,"keys":[{"key":"k"*20},{"key":"j"*20,"label":"工作"}]})
cfg = m.load_config()
T("active 越界收敛为 0", cfg["active"]==0)
T("无 label 的 key 补空串", cfg["keys"][0]["label"]=="" and cfg["keys"][1]["label"]=="工作")
m.save_config({"lang":"de","active":0,"keys":[]})
T("非法语言回退", m.load_config()["lang"] in m.LANG_CODES)
m.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "nope.json")     # 配置文件不存在
try:
    cfg = m.load_config(); T("配置缺失不崩溃且给空 keys", cfg["keys"]==[])
except Exception as e:
    T("配置缺失不崩溃", False, str(e))

print("== 2 key 校验与增删切换 ==")
m = load()
cases = {"oc_sk_"+"a"*16: True, "sk-"+"b"*16: True, "x"*16: True,
         "x"*15: False, "a b"*6: False, "abc…def": False,
         '"oc_sk_'+"a"*10+'"': True}   # 带引号会被剥掉后接受，属预期
for s, want in cases.items():
    got = m.valid_key(s)
    T(f"valid_key({s[:10]}…)→{want}", (got is not None)==want)
m.save_config({"lang":"zh","active":0,"keys":[]})
m.store_key("oc_sk_"+"a"*20); m.store_key("oc_sk_"+"b"*20); m.store_key("oc_sk_"+"c"*20)
cfg = m.load_config()
T("三把 key 入库", len(cfg["keys"])==3 and cfg["active"]==2)
m.act_use(0); T("use(0) 切换", m.load_config()["active"]==0)
m.CONFIG_PATH = m.CONFIG_PATH; m.LANG="zh"
m.act_delete(); cfg = m.load_config()
T("删除后剩 2 把且 active 归 0（删的是 a）", len(cfg["keys"])==2 and cfg["active"]==0 and cfg["keys"][0]["key"].endswith("b"*20))
m.act_delete(); m.act_delete(); m.act_delete()
T("空了再删不崩溃", m.load_config()["keys"]==[])

print("== 3 重命名（注入面） ==")
m = load()
m.save_config({"lang":"zh","active":0,"keys":[{"key":"oc_sk_"+"a"*20}]})
m.ask = lambda *a, **k: '主|号--注释"引号'
m.act_rename()
cfg = m.load_config()
label = cfg["keys"][0]["label"]
T("标签不含竖线", "|" not in label, repr(label))
T("标签不以 - 开头", not label.startswith("-"), repr(label))
T("标签长度 ≤24", len(label)<=24, str(len(label)))
out = grab(m, lambda: m.key_block(cfg))
lines = [l.split(" | ")[0] for l in out.splitlines() if l.strip()]
T("含竖线的标签已消毒", "|" not in "".join(lines), repr(lines[:2]))
m.ask = lambda *a, **k: "--我-的key"
m.act_rename(); cfg = m.load_config()
T("前导横线被剥掉", not cfg["keys"][0]["label"].startswith("-"), repr(cfg["keys"][0]["label"]))
T("key_title 消毒掩码", "|" not in m.key_title(cfg["keys"][0]))

print("== 4 状态机与缓存兼容 ==")
m = load()
m.save_config({"lang":"zh","active":0,"keys":[{"key":"oc_sk_"+"a"*20}]})
m.fetch_go = lambda k: ([("rolling",2.0,12,"3 小时",0.0,"x"),("weekly",72.0,30,"2 天",0.2,"y"),("monthly",83.0,60,"1 天",0.0,"z")], "10 月 24 日", None)
m.fetch_zen_model_count = lambda k: 29
out = grab(m, lambda: m.main())
T("Go 渲染含三行标签", out.count("本周")==1 and "83%" in out)
m.fetch_go = lambda k: (None, "", None)
out = grab(m, lambda: m.main()); T("空窗口 → Zen", "Zen" in out)
m.fetch_go = lambda k: (None, "", "网络不通")
m.save_cache("zen", models=29)                    # 先造一个 zen 缓存
out = grab(m, lambda: m.main()); T("断网时 Zen 走缓存", "按量付费" in out and "读取失败" in out)
# 旧格式缓存（无 kind、rows 为列表）
m.CACHE_PATH = os.path.join(m.DATA_DIR, "old.json")
json.dump({"ts": __import__("time").time()-120, "renews": "Oct 24",
           "rows": [["rolling", 2.0, 12, "3 小时"], ["weekly", 72.0, 30, "2 天"], ["monthly", 83.0, 60, "1 天"]]},
          open(m.CACHE_PATH, "w"))
m.fetch_go = lambda k: (None, "", "HTTP 502")
out = grab(m, lambda: m.main())
T("旧缓存(无kind)也能回退为 Go 卡片", "OpenCode Go" in out and "读取失败" in out)
m.fetch_go = lambda k: ([], "", None)
out = grab(m, lambda: m.main()); T("200 空窗口仍判 Zen", "Zen" in out)

print("== 5 通知阈值与去重 ==")
m = load(); sent=[]; m.notify = lambda t,b: sent.append(1)
rows = [("weekly", 84.9, 30, "2 天", 0.0, "R1")]
m.maybe_notify(rows); T("84.9% 不通知", len(sent)==0)
rows[0] = ("weekly", 85.0, 30, "2 天", 0.0, "R1")
for _ in range(3): m.maybe_notify(rows)
T("85% 只通知一次", len(sent)==1)
m.maybe_notify([("weekly", 90.0, 30, "2 天", 0.0, "R2")])
T("新周期再通知", len(sent)==2)

print("== 6 条形渲染 ==")
m = load()
for pct, want in ((0,0),(1,1),(50,25),(72,36),(83,42),(100,50)):
    s = m.bar_line(pct)
    lit = sum(1 for c in re.findall(r"48;5;(\d+)m", s) if c != "250")
    T(f"{pct}% 亮 {want} 段", lit==want, str(lit))

print("== 7 语言 ==")
m = load()
for code in m.LANG_CODES:
    m.LANG = code
    ok = all(k in m.STRINGS[code] for k in m.STRINGS["en"])
    T(f"{code} 键齐全", ok)
m.LANG = "fr"
T("法语日期", m.fmt_date(datetime.datetime(2026,10,24,tzinfo=datetime.timezone.utc))=="24 oct.")
T("法语时长", m.human(245400)=="2 j 20 h")
m.LANG = "ja"
T("日语时长", m.human(245400)=="2日20時間")

print("== 8 参数解析 ==")
m = load()
T("param 形式解析", m.normalize_args(["param0=--use","param1=2"])==["--use","2"])
T("裸参数透传", m.normalize_args(["--lang","zh-Hant"])==["--lang","zh-Hant"])
T("插件名解析", m.plugin_name()==os.path.basename(SRC).split(".")[0], m.plugin_name())

print("\n失败项:", FAILS if FAILS else "无")
