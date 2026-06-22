import os
import json
import re
import openai  # 使用旧版 openai（0.28.x）
from flask import Flask, request, render_template_string, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ============================================================
# 通用 Session
# ============================================================
def build_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"})
    return session

SESSION = build_session()

# ============================================================
# 工具函数：从输入中提取 Polymarket Slug
# ============================================================
def extract_slug(input_str):
    """自动从完整 URL 或直接 slug 中提取事件标识符"""
    if not input_str:
        return ""
    if 'polymarket.com' in input_str:
        # 匹配 /event/ 后面的部分，直到遇到 ? 或 #
        match = re.search(r'/event/([^?#]+)', input_str)
        if match:
            return match.group(1).strip('/')
        # 如果没有 /event/，则取最后一段
        return input_str.rstrip('/').split('/')[-1]
    # 如果已经是纯 slug，直接返回
    return input_str

# ============================================================
# 新闻 API
# ============================================================
def get_news(token, theme="iran-me", window="7d", limit=100, q=None):
    url = "https://news.ruilisi.com/api/v1/news"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"theme": theme, "window": window, "limit": limit}
    if q:
        params["q"] = q
    resp = SESSION.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def format_news_for_prompt(news, max_items=25):
    items = news.get("items", [])
    if not isinstance(items, list):
        return "新闻数据格式异常"
    formatted = []
    for i, item in enumerate(items[:max_items], start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("title_zh") or item.get("title_en") or "无标题"
        source = item.get("source") or item.get("source_name") or "未知来源"
        published_at = item.get("published_at") or item.get("publishedAt") or "未知时间"
        summary = item.get("summary") or item.get("description") or item.get("content") or ""
        url = item.get("url") or ""
        formatted.append(f"[N{i}]\n时间: {published_at}\n来源: {source}\n标题: {title}\n摘要: {str(summary)[:1200]}\n链接: {url}".strip())
    return "\n\n".join(formatted) if formatted else "没有可用新闻。"

# ============================================================
# Polymarket API
# ============================================================
def get_polymarket_data(event_slug):
    url = f"https://gamma-api.polymarket.com/markets/slug/{event_slug}"
    resp = SESSION.get(url, timeout=20)
    resp.raise_for_status()
    market = resp.json()
    clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
    outcome_prices = json.loads(market.get("outcomePrices", "[]"))
    return {
        "question": market.get("question", ""),
        "yes_token_id": str(clob_token_ids[0]) if clob_token_ids else None,
        "no_token_id": str(clob_token_ids[1]) if len(clob_token_ids) > 1 else None,
        "condition_id": market.get("conditionId"),
        "yes_price": float(outcome_prices[0]) if outcome_prices else None,
        "no_price": float(outcome_prices[1]) if len(outcome_prices) > 1 else None,
        "volume": market.get("volume"),
        "end_date": market.get("endDate"),
        "description": market.get("description"),
    }

def get_orderbook(token_id):
    url = "https://clob.polymarket.com/book"
    resp = SESSION.get(url, params={"token_id": str(token_id)}, timeout=20)
    resp.raise_for_status()
    return resp.json()

def get_price_history(token_id, interval="1m", fidelity=60):
    url = "https://clob.polymarket.com/prices-history"
    resp = SESSION.get(url, params={"market": str(token_id), "interval": interval, "fidelity": fidelity}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("history", [])

def get_trade_history(condition_id, limit=500):
    url = "https://data-api.polymarket.com/trades"
    params = {"market": condition_id, "limit": min(limit, 10000), "offset": 0, "takerOnly": "true"}
    resp = SESSION.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()

# ============================================================
# 数据摘要函数（全动态适配目标价）
# ============================================================
def safe_float(x):
    try:
        return float(x)
    except:
        return None

def summarize_orderbook(bids, asks, target_price, top_n=10):
    bid_levels = []
    for b in bids:
        p, s = safe_float(b.get("price")), safe_float(b.get("size"))
        if p is not None and s is not None:
            bid_levels.append({"price": p, "size": s, "notional": p*s})
    ask_levels = []
    for a in asks:
        p, s = safe_float(a.get("price")), safe_float(a.get("size"))
        if p is not None and s is not None:
            ask_levels.append({"price": p, "size": s, "notional": p*s})
    
    bid_levels_sorted = sorted(bid_levels, key=lambda x: x["price"], reverse=True)
    ask_levels_sorted = sorted(ask_levels, key=lambda x: x["price"])
    
    best_bid = bid_levels_sorted[0]["price"] if bid_levels_sorted else None
    best_ask = ask_levels_sorted[0]["price"] if ask_levels_sorted else None
    spread = None if best_bid is None or best_ask is None else best_ask - best_bid
    
    ask_until_target = [x for x in ask_levels_sorted if x["price"] <= target_price]
    cost_to_target = sum(x["notional"] for x in ask_until_target)
    shares_to_target = sum(x["size"] for x in ask_until_target)
    
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_levels": len(bid_levels_sorted),
        "ask_levels": len(ask_levels_sorted),
        "top_bids": bid_levels_sorted[:top_n],
        "top_asks": ask_levels_sorted[:top_n],
        "total_bid_size_top": sum(x["size"] for x in bid_levels_sorted[:top_n]),
        "total_ask_size_top": sum(x["size"] for x in ask_levels_sorted[:top_n]),
        "ask_size_until_target": shares_to_target,
        "estimated_cost_to_lift_to_target": cost_to_target,
        "ask_levels_until_target": ask_until_target[:30]
    }

def summarize_price_history(price_history, target_price):
    if not price_history:
        return {"count": 0, "latest_price": None, "ever_reached_target": False}
    prices = []
    for row in price_history:
        try:
            prices.append({"t": row.get("t"), "p": float(row.get("p"))})
        except:
            continue
    if not prices:
        return {"count": len(price_history), "latest_price": None, "ever_reached_target": False}
    first, latest = prices[0]["p"], prices[-1]["p"]
    min_p, max_p = min(x["p"] for x in prices), max(x["p"] for x in prices)
    return {
        "count": len(prices),
        "first_price": first,
        "latest_price": latest,
        "min_price": min_p,
        "max_price": max_p,
        "change_abs": latest - first,
        "change_pct": ((latest / first - 1) * 100) if first else None,
        "ever_reached_target": max_p >= target_price,
        "distance_to_target_abs": target_price - latest,
        "distance_to_target_pct": ((target_price / latest - 1) * 100) if latest else None,
        "recent_points": prices[-120:]
    }

def summarize_trades(trades):
    if not trades:
        return {"count": 0, "recent_trades": []}
    yes_trades, no_trades, large_trades = [], [], []
    yes_size, no_size, total_size = 0, 0, 0
    yes_price_size_sum, no_price_size_sum = 0, 0
    for t in trades:
        try:
            outcome = str(t.get("outcome", "")).lower()
            price, size = float(t.get("price", 0)), float(t.get("size", 0))
            total_size += size
            item = {
                "timestamp": t.get("timestamp"),
                "outcome": outcome,
                "side": t.get("side"),
                "price": price,
                "size": size,
                "notional": price*size
            }
            if price*size >= 500:
                large_trades.append(item)
            if outcome == "yes":
                yes_trades.append(item)
                yes_size += size
                yes_price_size_sum += price*size
            elif outcome == "no":
                no_trades.append(item)
                no_size += size
                no_price_size_sum += price*size
        except:
            continue
    return {
        "count": len(trades),
        "yes_trade_count": len(yes_trades),
        "no_trade_count": len(no_trades),
        "total_size": total_size,
        "yes_size": yes_size,
        "no_size": no_size,
        "yes_vwap": yes_price_size_sum/yes_size if yes_size>0 else None,
        "no_vwap": no_price_size_sum/no_size if no_size>0 else None,
        "large_trades": large_trades[-20:],
        "recent_trades": trades[-120:]
    }

# ============================================================
# DeepSeek 调用（适配旧版 openai）
# ============================================================
def call_deepseek(api_key, messages, max_retries=3):
    openai.api_key = api_key
    openai.base_url = "https://api.deepseek.com"   # 旧版用 api_base 也可以
    # 如果上述无效，可以改为 openai.api_base = "https://api.deepseek.com"
    last_error = None
    for attempt in range(1, max_retries+1):
        try:
            response = openai.ChatCompletion.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.15,
                max_tokens=7000
            )
            final_answer = response.choices[0].message.content
            data = json.loads(final_answer)
            if "detailed_reasoning" not in data or len(data.get("detailed_reasoning", "")) < 200:
                raise ValueError("推理太短")
            return data
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": final_answer if 'final_answer' in locals() else ""})
                messages.append({"role": "user", "content": f"上一个回答不合格：{e}。请重新输出严格 JSON，确保 detailed_reasoning 足够长且包含具体数据。"})
    raise ValueError(f"连续失败：{last_error}")

# ============================================================
# 前端 HTML（完整界面）
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>🎯 动态 Polymarket 预测分析器</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f6f8fc; padding: 2rem 1.5rem; line-height: 1.6; }
        .container { max-width: 1100px; margin: 0 auto; background: white; border-radius: 28px; box-shadow: 0 20px 60px rgba(0,20,40,0.08); padding: 2.5rem 2.8rem; }
        h1 { font-size: 2rem; font-weight: 700; margin-bottom: 0.2rem; display: flex; align-items: center; gap: 0.6rem; }
        .subtitle { color: #5a6d82; margin-bottom: 2rem; border-left: 4px solid #3b82f6; padding-left: 1.2rem; background: #f0f5ff; border-radius: 0 12px 12px 0; padding: 0.6rem 1.2rem; }
        .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.2rem 2rem; background: #f9fbfe; padding: 1.8rem 2rem; border-radius: 20px; margin-bottom: 1.8rem; }
        .form-group { display: flex; flex-direction: column; gap: 0.3rem; }
        .form-group.full-width { grid-column: 1 / -1; }
        .form-group label { font-weight: 600; font-size: 0.9rem; color: #2c3e50; }
        .form-group input, .form-group select { padding: 0.7rem 1rem; border: 1.5px solid #dce2ec; border-radius: 12px; font-size: 0.95rem; transition: 0.2s; background: white; }
        .form-group input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 4px rgba(59,130,246,0.12); }
        .form-hint { font-size: 0.78rem; color: #7a8aa0; }
        .btn-primary { grid-column: 1 / -1; background: #1a2639; color: white; border: none; padding: 0.9rem; border-radius: 14px; font-size: 1.05rem; font-weight: 600; cursor: pointer; transition: 0.2s; display: flex; justify-content: center; gap: 0.6rem; }
        .btn-primary:hover { background: #0f1a2e; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(26,38,57,0.2); }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .spinner { display: none; width: 20px; height: 20px; border: 3px solid rgba(255,255,255,0.2); border-top: 3px solid white; border-radius: 50%; animation: spin 0.8s linear infinite; }
        .loading .spinner { display: inline-block; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .error-msg { background: #fee2e2; color: #991b1b; padding: 0.8rem 1.4rem; border-radius: 12px; border-left: 4px solid #dc2626; display: none; margin-bottom: 1rem; }
        .error-msg.active { display: block; }
        #result-area { display: none; margin-top: 2rem; border-top: 2px solid #eef2f7; padding-top: 2rem; }
        #result-area.active { display: block; }
        .prob-card { display: flex; flex-wrap: wrap; gap: 1.2rem 2.5rem; background: linear-gradient(135deg, #f0f7ff, white); padding: 1.8rem 2.2rem; border-radius: 20px; border: 1px solid #e5edf8; margin-bottom: 2rem; align-items: center; }
        .prob-item .label { font-size: 0.75rem; text-transform: uppercase; color: #6b7f98; font-weight: 600; }
        .prob-item .value { font-size: 2.2rem; font-weight: 700; }
        .prob-item .value.low { color: #3b82f6; } .prob-item .value.mid { color: #7c3aed; } .prob-item .value.high { color: #dc2626; }
        .prob-divider { width: 1px; height: 3rem; background: #dce2ec; }
        .one-sentence { background: #f0f5ff; padding: 1rem 1.8rem; border-radius: 14px; font-weight: 500; border-left: 5px solid #3b82f6; margin-bottom: 1.8rem; }
        .section { margin-bottom: 2.2rem; }
        .section h3 { font-size: 1.2rem; font-weight: 600; margin-bottom: 0.8rem; display: flex; gap: 0.5rem; }
        .section .content { background: #f9fbfe; padding: 1.2rem 1.6rem; border-radius: 16px; border: 1px solid #eef2f7; white-space: pre-wrap; }
        .evidence-grid { display: flex; flex-direction: column; gap: 1rem; }
        .evidence-item { background: white; padding: 1rem 1.4rem; border-radius: 14px; border: 1px solid #eef2f7; }
        .evidence-item .meta { display: flex; flex-wrap: wrap; gap: 0.4rem 1.2rem; font-size: 0.8rem; color: #6b7f98; margin-bottom: 0.3rem; }
        .tag { padding: 0.05rem 0.7rem; border-radius: 40px; font-weight: 500; }
        .tag.positive { background: #d1fae5; color: #065f46; }
        .tag.negative { background: #fee2e2; color: #991b1b; }
        .tag.neutral { background: #fef3c7; color: #92400e; }
        .decision-box { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; background: #f9fbfe; padding: 1.2rem 1.6rem; border-radius: 16px; border: 1px solid #eef2f7; }
        .risk-tags { display: flex; flex-wrap: wrap; gap: 0.5rem; }
        .risk-tag { background: #fef2f2; color: #991b1b; padding: 0.2rem 1rem; border-radius: 40px; border: 1px solid #fecaca; }
        .json-toggle { background: none; border: none; color: #3b82f6; font-weight: 600; cursor: pointer; padding: 0.2rem 0; }
        .json-box { display: none; background: #0f1a2e; color: #e2e8f0; padding: 1.2rem 1.6rem; border-radius: 14px; overflow-x: auto; white-space: pre-wrap; max-height: 400px; overflow-y: auto; margin-top: 0.6rem; }
        .json-box.open { display: block; }
        @media (max-width: 720px) { .container { padding: 1.5rem; } .form-grid { grid-template-columns: 1fr; } .decision-box { grid-template-columns: 1fr; } .prob-card { flex-direction: column; align-items: flex-start; } .prob-divider { display: none; } }
    </style>
</head>
<body>
<div class="container">
    <h1>🎯 动态阈值预测分析器 <small style="font-size:1rem;font-weight:400;color:#6b7f98;">可自定义目标价</small></h1>
    <div class="subtitle">输入凭证、事件 Slug 和<strong>你想要预测的目标价格</strong>（例如 0.60, 0.75, 0.90），系统将评估 Yes 价格触及该阈值的概率。</div>
    
    <form id="analyze-form" class="form-grid">
        <div class="form-group"><label>🔑 DeepSeek API Key</label><input type="password" id="deepseek-key" placeholder="sk-..." required /></div>
        <div class="form-group"><label>📰 News API Token</label><input type="password" id="news-token" placeholder="nrk_..." required /></div>
        <div class="form-group full-width"><label>🔗 Polymarket 事件 Slug</label><input type="text" id="event-input" placeholder="iran-agrees-to-end-enrichment..." required /></div>
        <div class="form-group full-width"><label>🎯 目标价格 (Threshold)</label><input type="number" id="target-price" value="0.60" step="0.01" min="0.01" max="0.99" required /></div>
        <button type="submit" class="btn-primary" id="submit-btn"><span class="spinner"></span><span class="btn-text">🚀 开始动态分析</span></button>
    </form>

    <div class="error-msg" id="error-msg"></div>
    <div id="result-area">
        <div class="prob-card" id="prob-card">
            <div class="prob-item"><span class="label">🔽 低估值</span><span class="value low" id="prob-low">--<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span></span></div>
            <div class="prob-divider"></div>
            <div class="prob-item"><span class="label">📊 中位数</span><span class="value mid" id="prob-mid">--<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span></span></div>
            <div class="prob-divider"></div>
            <div class="prob-item"><span class="label">🔼 高估值</span><span class="value high" id="prob-high">--<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span></span></div>
        </div>
        <div class="one-sentence" id="one-sentence">等待分析…</div>
        <div class="section"><h3>📌 操作建议</h3><div class="decision-box" id="decision-box"><div class="item"><span class="lbl" style="font-size:0.7rem;text-transform:uppercase;color:#6b7f98;">动作</span><span class="val" id="dec-action" style="font-weight:600;">--</span></div><div class="item"><span class="lbl" style="font-size:0.7rem;text-transform:uppercase;color:#6b7f98;">理由</span><span class="val" id="dec-reason" style="font-weight:600;">--</span></div><div class="item"><span class="lbl" style="font-size:0.7rem;text-transform:uppercase;color:#6b7f98;">入场计划</span><span class="val" id="dec-entry" style="font-weight:600;">--</span></div></div></div>
        <div class="section"><h3>📈 价格触发条件</h3><div class="content" id="price-triggers"></div></div>
        <div class="section"><h3>🧠 详细推理 <span class="count" id="reasoning-len" style="font-weight:400;font-size:0.9rem;color:#6b7f98;"></span></h3><div class="content" id="detailed-reasoning" style="max-height:400px;overflow-y:auto;"></div></div>
        <div class="section"><h3>📊 市场证据 <span class="count" id="market-count" style="font-weight:400;font-size:0.9rem;color:#6b7f98;"></span></h3><div class="evidence-grid" id="market-evidence"></div></div>
        <div class="section"><h3>📰 新闻证据 <span class="count" id="news-count" style="font-weight:400;font-size:0.9rem;color:#6b7f98;"></span></h3><div class="evidence-grid" id="news-evidence"></div></div>
        <div class="section"><h3>⚠️ 关键风险</h3><div class="risk-tags" id="risk-tags"></div></div>
        <div class="section"><button class="json-toggle" id="json-toggle">📄 查看完整 JSON</button><div class="json-box" id="json-box"></div></div>
    </div>
</div>
<script>
    const form = document.getElementById('analyze-form');
    const submitBtn = document.getElementById('submit-btn');
    const errorMsg = document.getElementById('error-msg');
    const resultArea = document.getElementById('result-area');
    
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        errorMsg.classList.remove('active');
        resultArea.classList.remove('active');
        submitBtn.disabled = true;
        submitBtn.classList.add('loading');
        
        const payload = {
            deepseek_key: document.getElementById('deepseek-key').value.trim(),
            news_token: document.getElementById('news-token').value.trim(),
            event_slug: document.getElementById('event-input').value.trim(),
            target_price: parseFloat(document.getElementById('target-price').value)
        };
        if (!payload.deepseek_key || !payload.news_token || !payload.event_slug || !payload.target_price) {
            errorMsg.textContent = '请完整填写所有字段';
            errorMsg.classList.add('active');
            submitBtn.disabled = false; submitBtn.classList.remove('loading'); return;
        }
        try {
            const resp = await fetch('/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || '请求失败');
            renderResult(data);
        } catch (err) {
            errorMsg.textContent = '❌ ' + err.message;
            errorMsg.classList.add('active');
        } finally {
            submitBtn.disabled = false;
            submitBtn.classList.remove('loading');
        }
    });

    function renderResult(data) {
        const prob = data.probability_yes_reaches_target || data.probability_yes_reaches_60 || {};
        document.getElementById('prob-low').innerHTML = (prob.low ?? '--') + '<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span>';
        document.getElementById('prob-mid').innerHTML = (prob.mid ?? '--') + '<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span>';
        document.getElementById('prob-high').innerHTML = (prob.high ?? '--') + '<span style="font-size:1.2rem;font-weight:500;color:#4b5d73;">%</span>';
        document.getElementById('one-sentence').textContent = data.final_answer_one_sentence || '';
        const dec = data.decision || {};
        document.getElementById('dec-action').textContent = dec.action || '--';
        document.getElementById('dec-reason').textContent = dec.reason || '--';
        document.getElementById('dec-entry').textContent = dec.entry_plan || '--';
        const triggers = data.price_triggers || {};
        document.getElementById('price-triggers').innerHTML = Object.entries(triggers).map(([k,v]) => `<strong>${k}</strong>: ${v}`).join(' &nbsp;|&nbsp; ');
        const reasoning = data.detailed_reasoning || '';
        document.getElementById('detailed-reasoning').textContent = reasoning;
        document.getElementById('reasoning-len').textContent = reasoning.length + ' 字';
        
        const renderEvidence = (items, containerId, countId) => {
            const container = document.getElementById(containerId); container.innerHTML = '';
            document.getElementById(countId).textContent = items.length + ' 条';
            items.forEach((item, idx) => {
                const div = document.createElement('div'); div.className = 'evidence-item';
                const impact = item.impact_on_yes_reaches_target || item.impact_on_yes_reaches_60 || 'neutral';
                const cls = {positive:'positive', negative:'negative', neutral:'neutral'}[impact] || 'neutral';
                div.innerHTML = `<div class="meta"><span>#${idx+1}</span><span class="tag ${cls}">${impact}</span></div>
                    <div class="title">${item.evidence || item.title || ''}</div>
                    <div class="explanation">${item.explanation || ''}</div>`;
                container.appendChild(div);
            });
        };
        renderEvidence(data.market_evidence || [], 'market-evidence', 'market-count');
        renderEvidence(data.news_evidence || [], 'news-evidence', 'news-count');
        
        const risks = data.key_risks || [];
        const riskContainer = document.getElementById('risk-tags'); riskContainer.innerHTML = '';
        risks.forEach(r => { const span = document.createElement('span'); span.className = 'risk-tag'; span.textContent = r; riskContainer.appendChild(span); });
        if (!risks.length) riskContainer.innerHTML = '<span style="color:#6b7f98;">未识别到关键风险</span>';
        
        document.getElementById('json-box').textContent = JSON.stringify(data, null, 2);
        resultArea.classList.add('active');
        resultArea.scrollIntoView({ behavior: 'smooth' });
    }
    document.getElementById('json-toggle').addEventListener('click', function() {
        const box = document.getElementById('json-box');
        box.classList.toggle('open');
        this.textContent = box.classList.contains('open') ? '📄 收起 JSON' : '📄 查看完整 JSON';
    });
</script>
</body>
</html>
"""

# ============================================================
# Flask 路由
# ============================================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    deepseek_key = data.get('deepseek_key')
    news_token = data.get('news_token')
    raw_slug = data.get('event_slug')
    target_price = float(data.get('target_price', 0.60))

    if not deepseek_key or not news_token or not raw_slug:
        return jsonify({"error": "缺少必要参数"}), 400

    # 自动提取 slug（如果是完整 URL）
    event_slug = extract_slug(raw_slug)

    try:
        # 1. Polymarket 数据
        pm_data = get_polymarket_data(event_slug)
        yes_token = pm_data["yes_token_id"]
        condition_id = pm_data["condition_id"]
        current_yes = pm_data["yes_price"]
        required_gain_pct = ((target_price / current_yes - 1) * 100) if current_yes else None

        # 2. 订单簿
        orderbook = get_orderbook(yes_token)
        orderbook_summary = summarize_orderbook(orderbook.get("bids", []), orderbook.get("asks", []), target_price)

        # 3. 历史价格
        try:
            price_history = get_price_history(yes_token)
            price_history_summary = summarize_price_history(price_history, target_price)
        except:
            price_history_summary = {"count": 0, "latest_price": None, "ever_reached_target": False}

        # 4. 交易记录
        try:
            trades = get_trade_history(condition_id, limit=500)
            trade_summary = summarize_trades(trades)
        except:
            trade_summary = {"count": 0}

        # 5. 新闻
        try:
            news = get_news(news_token, q="Iran uranium nuclear")
            news_text = format_news_for_prompt(news)
        except:
            news_text = "新闻获取失败"

        # 6. 构建 Prompt
        system_prompt = f"""
你是一位预测市场分析师。核心任务：评估 Yes 价格在该市场结束前达到或超过 ${target_price} 的概率。
注意：这不是问最终是否 Yes 结算，而是市场价格是否会涨到 >= ${target_price}。
输出严格 JSON，包含：
{{
  "probability_yes_reaches_target": {{ "low": 数字, "mid": 数字, "high": 数字 }},
  "decision": {{ "action": "buy_now/wait/avoid", "reason": "", "entry_plan": "" }},
  "price_triggers": {{ "consider_buy_below": 数字, "neutral_zone": "", "avoid_chasing_above": 数字 }},
  "market_evidence": [ {{ "evidence": "含具体数字", "impact_on_yes_reaches_target": "positive/negative/neutral", "explanation": "至少80字" }} ],
  "news_evidence": [ {{ "news_id": "N1", "title": "", "source": "", "published_at": "", "impact_on_yes_reaches_target": "positive/negative/neutral", "explanation": "至少100字" }} ],
  "key_risks": [ "字符串" ],
  "detailed_reasoning": "不少于500字",
  "final_answer_one_sentence": "字符串"
}}
强制要求：market_evidence 至少5条，news_evidence 至少5条，必须引用 N 编号新闻。
"""

        user_message = f"""
## 核心问题
P(Yes 价格 >= ${target_price})？
当前 Yes 价格: {pm_data['yes_price']:.3f} ({pm_data['yes_price']*100:.1f}%)
目标: ${target_price}，需上涨 {required_gain_pct:.1f}%

## 市场数据
事件: {pm_data['question']}
总交易量: {float(pm_data.get('volume',0))}
到期日: {pm_data.get('end_date','未知')}

## 订单簿摘要
{json.dumps(orderbook_summary, ensure_ascii=False, default=str, indent=2)}

## 历史价格摘要
{json.dumps(price_history_summary, ensure_ascii=False, default=str, indent=2)}

## 交易记录摘要
{json.dumps(trade_summary, ensure_ascii=False, default=str, indent=2)}

## 新闻证据
{news_text}

请分析。
"""

        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]
        result = call_deepseek(deepseek_key, messages)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
