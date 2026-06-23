import os
import json
import re
import requests
import traceback
from flask import Flask, request, render_template_string, jsonify, make_response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ============================================================
# JSON 错误响应：避免 Flask 默认返回 HTML 错误页
# ============================================================
def json_error(message, status=500, detail=None):
    payload = {"error": str(message)}
    if detail:
        payload["detail"] = str(detail)[:2000]
    response = make_response(jsonify(payload), status)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response

@app.errorhandler(400)
def handle_400(e):
    return json_error("请求格式错误，请检查前端提交的 JSON。", 400, e)

@app.errorhandler(404)
def handle_404(e):
    return json_error("接口不存在，请检查请求路径。", 404, e)

@app.errorhandler(405)
def handle_405(e):
    return json_error("请求方法不允许，请使用 POST 调用 /analyze。", 405, e)

@app.errorhandler(500)
def handle_500(e):
    return json_error("服务器内部错误。", 500, e)

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.exception("Unhandled exception")
    return json_error("服务器发生未捕获异常。", 500, traceback.format_exc())


# ============================================================
# 通用 Session
# ============================================================
def build_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET", "POST"])
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
    if not input_str:
        return ""
    if 'polymarket.com' in input_str:
        match = re.search(r'/event/([^?#]+)', input_str)
        if match:
            return match.group(1).strip('/')
        return input_str.rstrip('/').split('/')[-1]
    return input_str.strip()

# ============================================================
# 新闻 API
# ============================================================
def get_news(token, theme="iran-me", window="30d", limit=100, q=None):
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
        formatted.append(
            f"[N{i}]\n时间: {published_at}\n来源: {source}\n标题: {title}\n"
            f"摘要: {str(summary)[:1200]}\n链接: {url}".strip()
        )
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
# 数据摘要函数
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
        "best_bid": best_bid, "best_ask": best_ask, "spread": spread,
        "bid_levels": len(bid_levels_sorted), "ask_levels": len(ask_levels_sorted),
        "top_bids": bid_levels_sorted[:top_n], "top_asks": ask_levels_sorted[:top_n],
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
        "count": len(prices), "first_price": first, "latest_price": latest,
        "min_price": min_p, "max_price": max_p,
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
            item = {"timestamp": t.get("timestamp"), "outcome": outcome,
                    "side": t.get("side"), "price": price, "size": size, "notional": price*size}
            if price*size >= 500:
                large_trades.append(item)
            if outcome == "yes":
                yes_trades.append(item); yes_size += size; yes_price_size_sum += price*size
            elif outcome == "no":
                no_trades.append(item); no_size += size; no_price_size_sum += price*size
        except:
            continue
    return {
        "count": len(trades),
        "yes_trade_count": len(yes_trades), "no_trade_count": len(no_trades),
        "total_size": total_size, "yes_size": yes_size, "no_size": no_size,
        "yes_vwap": yes_price_size_sum/yes_size if yes_size > 0 else None,
        "no_vwap": no_price_size_sum/no_size if no_size > 0 else None,
        "large_trades": large_trades[-20:], "recent_trades": trades[-120:]
    }

# ============================================================
# AI 调用层：支持多种 Provider
# ============================================================

def call_openai_compatible(api_key, base_url, model, messages, temperature=0.15, max_tokens=4500):
    """通用 OpenAI 兼容接口调用（DeepSeek / ChatGPT / 豆包）"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    resp = SESSION.post(f"{base_url.rstrip('/')}/chat/completions",
                        headers=headers, json=payload, timeout=75)
    if not resp.ok:
        raise ValueError(f"AI 接口 HTTP {resp.status_code}: {resp.text[:1000]}")
    try:
        data = resp.json()
    except Exception:
        raise ValueError(f"AI 接口返回非 JSON: {resp.text[:1000]}")
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise ValueError(f"AI 返回结构异常: {str(data)[:1000]}")

def call_claude(api_key, messages, model="claude-sonnet-4-5", temperature=0.15, max_tokens=4500):
    """调用 Anthropic Claude API"""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }
    # 将 system 消息提取出来
    system_content = ""
    user_messages = []
    for m in messages:
        if m["role"] == "system":
            system_content = m["content"]
        else:
            user_messages.append(m)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": user_messages
    }
    if system_content:
        payload["system"] = system_content

    resp = SESSION.post("https://api.anthropic.com/v1/messages",
                        headers=headers, json=payload, timeout=75)
    if not resp.ok:
        raise ValueError(f"Claude 接口 HTTP {resp.status_code}: {resp.text[:1000]}")
    try:
        data = resp.json()
    except Exception:
        raise ValueError(f"Claude 接口返回非 JSON: {resp.text[:1000]}")
    try:
        return data["content"][0]["text"]
    except Exception:
        raise ValueError(f"Claude 返回结构异常: {str(data)[:1000]}")

# Provider 配置表
PROVIDER_CONFIG = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "type": "openai_compat"
    },
    "chatgpt": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "type": "openai_compat"
    },
    "claude": {
        "model": "claude-sonnet-4-5",
        "type": "claude"
    },
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "ep-20250101000000-xxxxx",  # 用户需填入自己的 endpoint
        "type": "openai_compat"
    }
}

# ============================================================
# 修复：call_ai 重试逻辑，正确保存 final_answer
# ============================================================

def parse_ai_json(raw):
    """尽量从模型输出中提取 JSON；支持 ```json、前后废话、纯 JSON。"""
    if not raw or not str(raw).strip():
        raise ValueError("AI 返回空内容")
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end+1])
        raise

def call_ai(provider, api_key, messages, custom_model=None, max_retries=2):
    cfg = PROVIDER_CONFIG.get(provider, PROVIDER_CONFIG["deepseek"])
    model = custom_model or cfg["model"]
    last_error = None
    last_raw = ""  # FIXED: 保存最后一次的原始输出

    for attempt in range(1, max_retries + 1):
        try:
            if cfg["type"] == "claude":
                raw = call_claude(api_key, messages, model=model)
            else:
                raw = call_openai_compatible(api_key, cfg["base_url"], model, messages)

            last_raw = raw  # FIXED: 保存原始文本

            data = parse_ai_json(raw)

            if "detailed_reasoning" not in data or len(data.get("detailed_reasoning", "")) < 200:
                raise ValueError("推理太短，需要更多详细分析")

            return data

        except Exception as e:
            last_error = e
            # FIXED: 将错误信息和上一次的输出（如果有）追加到消息中，以便 AI 修正
            error_msg = f"上一次回答格式错误或内容不足。错误：{e}"
            if last_raw:
                # 截断，避免消息过长
                truncated = last_raw[:500] + ("…" if len(last_raw) > 500 else "")
                error_msg += f"\n上一次的原始输出（截断）：\n{truncated}"
            # 更新消息列表，添加助手和用户的纠正提示
            messages = list(messages)
            messages.append({"role": "assistant", "content": last_raw if last_raw else "（空输出）"})
            messages.append({"role": "user", "content": f"请根据以上错误重新输出严格的 JSON，确保 detailed_reasoning 足够长且包含具体数据。不要输出 Markdown。错误详情：{e}"})
            # 如果已经是最后一次尝试，不再继续
            if attempt == max_retries:
                break

    # 所有重试都失败
    raise ValueError(f"AI 调用连续失败（重试 {max_retries} 次），最后错误：{last_error}")

# ============================================================
# Prompt 构建
# ============================================================
def build_prompts(pm_data, orderbook_summary, price_history_summary, trade_summary, news_text, target_price):
    current_yes = pm_data["yes_price"] or 0
    required_gain_pct = ((target_price / current_yes - 1) * 100) if current_yes else None

    system_prompt = f"""你是一位预测市场分析师，专门分析 Polymarket 二元事件市场。

你的核心任务是回答：

P(Yes 价格在该市场结束前达到或超过 {target_price} 美元) 是多少？

注意：
这不是问事件最终是否 Yes 结算。
这是问 Yes 市场价格是否会涨到 >= {target_price}。

你必须输出严格 JSON，不要输出 Markdown，不要输出免责声明，不要输出多余解释。

JSON 必须包含以下字段：

{{
  "probability_yes_reaches_target": {{
    "low": 数字,
    "mid": 数字,
    "high": 数字,
    "unit": "%"
  }},
  "current_market": {{
    "yes_price": 数字,
    "no_price": 数字,
    "volume": 数字,
    "orderbook_summary": "字符串"
  }},
  "decision": {{
    "action": "buy_now / wait / avoid / scale_in",
    "reason": "字符串",
    "entry_plan": "字符串"
  }},
  "price_triggers": {{
    "consider_buy_below": 数字,
    "neutral_zone": "字符串",
    "avoid_chasing_above": 数字
  }},
  "market_evidence": [
    {{
      "evidence": "字符串，必须包含具体数字",
      "impact_on_yes_reaches_target": "positive / negative / neutral",
      "explanation": "字符串，至少80个中文字"
    }}
  ],
  "news_evidence": [
    {{
      "news_id": "N1",
      "title": "字符串",
      "source": "字符串",
      "published_at": "字符串",
      "impact_on_yes_reaches_target": "positive / negative / neutral",
      "explanation": "字符串，至少100个中文字"
    }}
  ],
  "key_risks": ["字符串"],
  "detailed_reasoning": "不少于500个中文字的详细推理",
  "final_answer_one_sentence": "字符串"
}}

硬性要求：
1. probability_yes_reaches_target.low/mid/high 必须存在，mid 为 0-100 之间数字。
2. market_evidence 至少 5 条，每条必须引用具体数字。
3. news_evidence 至少 5 条，每条必须引用 N 编号。
4. detailed_reasoning 不少于 500 个中文字。
5. 不允许只说空话，必须引用具体数据和新闻。
6. 必须区分：事件最终Yes结算概率、当前价格隐含概率、Yes价格触及{target_price}的概率。
7. 必须分析群众情绪（FOMO/基本面/对冲）和市场微结构（订单簿/大单/VWAP）。
"""

    user_message = f"""## 核心问题

请估计：P(Yes 价格在该市场结束前达到或超过 {target_price} 美元)

当前 Yes 价格是 {pm_data['yes_price']:.3f}（即 {pm_data['yes_price'] * 100:.1f}%）。
目标价格是 {target_price}，需要上涨约 {required_gain_pct:.1f}%。

## Polymarket 市场数据

事件: {pm_data['question']}
Yes 当前价格: {pm_data['yes_price']}
No 当前价格: {pm_data['no_price']}
总交易量: {float(pm_data.get('volume', 0))}
到期日: {pm_data.get('end_date', '未知')}
规则/描述: {pm_data.get('description', '无')}

## 订单簿摘要

{json.dumps(orderbook_summary, ensure_ascii=False, default=str, indent=2)}

请分析：
1. 是否容易被小额资金推到 {target_price}；
2. {target_price} 附近是否有较强卖压；
3. 当前 spread 是否说明流动性不足；
4. 从当前价格到 {target_price} 需要多强的买盘推动。

## 历史价格摘要

{json.dumps(price_history_summary, ensure_ascii=False, default=str, indent=2)}

## 交易记录摘要

{json.dumps(trade_summary, ensure_ascii=False, default=str, indent=2)}

## 相关新闻证据

下面每条新闻都有编号，你必须在 news_evidence 中引用这些编号（N1、N2…）。

{news_text}

## 分析要求

1. 价格距离分析：当前 {pm_data['yes_price']:.3f} → 目标 {target_price}，需涨 {required_gain_pct:.1f}%，是否符合历史波动。
2. 历史价格：是否曾接近或超过 {target_price}。
3. 订单簿：盘口厚度、成本估算、能否被资金推高。
4. 成交分析：成交方向、大单分布、VWAP 与目标价关系。
5. 新闻分析：至少引用 5 条 N 编号新闻，说明每条对"Yes 价格触及 {target_price}"的影响方向。
6. 群众情绪：FOMO、投机、基本面支撑、对冲需求，情绪能否持续到触及目标价。

## 输出要求

只输出严格 JSON，不要输出 Markdown，不要输出免责声明，不要输出额外文字。
"""

    return [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": user_message.strip()}
    ]

# ============================================================
# HTML 模板（含前端修复）
# ============================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Polymarket 智能预测</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

  :root {
    --bg: #0a0e1a;
    --surface: #111827;
    --surface2: #1a2235;
    --border: #1e2d45;
    --accent: #00d4aa;
    --accent2: #7c6af7;
    --accent3: #f59e0b;
    --danger: #ef4444;
    --text: #e8edf5;
    --text2: #8899b4;
    --text3: #4a5d7a;
    --positive: #10b981;
    --negative: #ef4444;
    --neutral: #f59e0b;
    --font: 'Space Grotesk', system-ui, sans-serif;
    --mono: 'JetBrains Mono', monospace;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 0;
  }

  /* ── Header ── */
  .header {
    background: linear-gradient(135deg, #0d1b2e 0%, #0f2240 50%, #0a1628 100%);
    border-bottom: 1px solid var(--border);
    padding: 2rem 2.5rem 1.8rem;
    position: relative;
    overflow: hidden;
  }
  .header::before {
    content: '';
    position: absolute; inset: 0;
    background: radial-gradient(ellipse at 80% 50%, rgba(0,212,170,0.06) 0%, transparent 70%);
    pointer-events: none;
  }
  .header-inner { max-width: 1200px; margin: 0 auto; position: relative; }
  .logo-row { display: flex; align-items: center; gap: 1rem; margin-bottom: 0.5rem; }
  .logo-mark {
    width: 40px; height: 40px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: flex; align-items: center; justify-content: center;
    font-size: 1.2rem; font-weight: 700; color: #000;
  }
  .logo-text { font-size: 1.4rem; font-weight: 700; letter-spacing: -0.02em; }
  .logo-text span { color: var(--accent); }
  .header-desc { color: var(--text2); font-size: 0.88rem; max-width: 600px; line-height: 1.6; }

  /* ── Main layout ── */
  .main { max-width: 1200px; margin: 0 auto; padding: 2rem 2.5rem 4rem; }

  /* ── Config panel ── */
  .config-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 1.8rem 2rem;
    margin-bottom: 2rem;
  }
  .panel-title {
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--accent);
    margin-bottom: 1.4rem;
    display: flex; align-items: center; gap: 0.5rem;
  }
  .panel-title::before { content: ''; display: block; width: 16px; height: 2px; background: var(--accent); }

  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem 1.6rem;
  }

  .field { display: flex; flex-direction: column; gap: 0.4rem; }
  .field.full { grid-column: 1 / -1; }

  label {
    font-size: 0.78rem; font-weight: 600; color: var(--text2);
    letter-spacing: 0.04em; text-transform: uppercase;
  }

  input, select {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.65rem 0.9rem;
    color: var(--text);
    font-family: var(--font);
    font-size: 0.9rem;
    transition: border-color 0.2s, box-shadow 0.2s;
    outline: none;
    width: 100%;
  }
  input:focus, select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(0,212,170,0.12);
  }
  input[type="password"] { font-family: var(--mono); letter-spacing: 0.05em; }
  input::placeholder { color: var(--text3); }
  select option { background: var(--surface2); }

  .field-hint { font-size: 0.73rem; color: var(--text3); }

  /* Provider selector */
  .provider-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.6rem;
    grid-column: 1 / -1;
  }
  .provider-btn {
    border: 1px solid var(--border);
    background: var(--surface2);
    border-radius: 8px;
    padding: 0.7rem;
    cursor: pointer;
    text-align: center;
    transition: all 0.15s;
    color: var(--text2);
    font-family: var(--font);
    font-size: 0.82rem;
    font-weight: 500;
  }
  .provider-btn:hover { border-color: var(--accent2); color: var(--text); }
  .provider-btn.active {
    border-color: var(--accent);
    background: rgba(0,212,170,0.08);
    color: var(--accent);
    font-weight: 600;
  }
  .provider-icon { font-size: 1.2rem; display: block; margin-bottom: 0.2rem; }

  /* Analyze button */
  .btn-analyze {
    grid-column: 1 / -1;
    background: linear-gradient(135deg, var(--accent), #00b894);
    color: #000;
    border: none;
    border-radius: 10px;
    padding: 0.85rem 2rem;
    font-family: var(--font);
    font-size: 0.95rem;
    font-weight: 700;
    cursor: pointer;
    transition: all 0.2s;
    display: flex; align-items: center; justify-content: center; gap: 0.6rem;
    letter-spacing: 0.02em;
  }
  .btn-analyze:hover { transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,212,170,0.25); }
  .btn-analyze:disabled { opacity: 0.4; cursor: not-allowed; transform: none; box-shadow: none; }

  .spinner {
    display: none; width: 18px; height: 18px;
    border: 2px solid rgba(0,0,0,0.2); border-top: 2px solid #000;
    border-radius: 50%; animation: spin 0.7s linear infinite;
  }
  .loading .spinner { display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Error ── */
  .error-bar {
    background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3);
    color: #fca5a5; border-radius: 10px; padding: 0.8rem 1.2rem;
    font-size: 0.87rem; display: none; margin-bottom: 1.5rem;
  }
  .error-bar.show { display: block; }

  /* ── Result area ── */
  #result { display: none; }
  #result.show { display: block; }

  /* Probability hero */
  .prob-hero {
    background: linear-gradient(135deg, var(--surface) 0%, #0f1e35 100%);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 2rem 2.4rem;
    margin-bottom: 1.5rem;
    position: relative; overflow: hidden;
  }
  .prob-hero::after {
    content: '';
    position: absolute; top: 0; right: 0; bottom: 0; width: 200px;
    background: radial-gradient(ellipse at right center, rgba(0,212,170,0.05) 0%, transparent 70%);
    pointer-events: none;
  }
  .prob-label {
    font-size: 0.72rem; font-weight: 600; color: var(--text2);
    letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 1.2rem;
  }
  .prob-numbers { display: flex; align-items: flex-end; gap: 2.5rem; flex-wrap: wrap; }
  .prob-item {}
  .prob-item .pl { font-size: 0.7rem; color: var(--text3); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.2rem; }
  .prob-item .pv { font-family: var(--mono); font-size: 2.8rem; font-weight: 600; line-height: 1; }
  .pv.low { color: #60a5fa; }
  .pv.mid { color: var(--accent); }
  .pv.high { color: #f472b6; }
  .prob-divider { width: 1px; height: 3rem; background: var(--border); flex-shrink: 0; }
  .prob-sentence {
    margin-top: 1.2rem; padding: 0.9rem 1.2rem;
    background: rgba(0,212,170,0.05);
    border-left: 3px solid var(--accent);
    border-radius: 0 8px 8px 0;
    font-size: 0.9rem; color: var(--text); line-height: 1.6;
  }

  /* Decision grid */
  .decision-strip {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem;
    margin-bottom: 1.5rem;
  }
  .dec-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.2rem;
  }
  .dec-card .dk { font-size: 0.68rem; color: var(--text3); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.4rem; }
  .dec-card .dv { font-size: 0.9rem; font-weight: 500; color: var(--text); }
  .action-tag {
    display: inline-block; padding: 0.25rem 0.8rem;
    border-radius: 20px; font-size: 0.8rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  .action-buy_now { background: rgba(16,185,129,0.15); color: #34d399; }
  .action-wait { background: rgba(245,158,11,0.15); color: #fbbf24; }
  .action-avoid { background: rgba(239,68,68,0.15); color: #f87171; }
  .action-scale_in { background: rgba(124,106,247,0.15); color: #a78bfa; }

  /* Sections */
  .section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 1.6rem 1.8rem;
    margin-bottom: 1.2rem;
  }
  .section-title {
    font-size: 0.78rem; font-weight: 600; color: var(--text2);
    letter-spacing: 0.1em; text-transform: uppercase;
    display: flex; align-items: center; gap: 0.7rem;
    margin-bottom: 1.2rem;
  }
  .section-title .icon { font-size: 1rem; }
  .section-title .cnt { font-weight: 400; color: var(--text3); }

  /* Reasoning */
  .reasoning-text {
    font-size: 0.88rem; color: var(--text2); line-height: 1.8;
    white-space: pre-wrap; max-height: 400px; overflow-y: auto;
    padding-right: 0.5rem;
  }
  .reasoning-text::-webkit-scrollbar { width: 4px; }
  .reasoning-text::-webkit-scrollbar-track { background: transparent; }
  .reasoning-text::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

  /* Evidence cards */
  .evidence-list { display: flex; flex-direction: column; gap: 0.8rem; }
  .ev-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 0.9rem 1.1rem;
  }
  .ev-meta { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
  .ev-id { font-family: var(--mono); font-size: 0.72rem; color: var(--text3); }
  .ev-impact {
    padding: 0.1rem 0.6rem; border-radius: 20px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
  }
  .ev-impact.positive { background: rgba(16,185,129,0.12); color: #34d399; }
  .ev-impact.negative { background: rgba(239,68,68,0.12); color: #f87171; }
  .ev-impact.neutral { background: rgba(245,158,11,0.12); color: #fbbf24; }
  .ev-source { font-size: 0.72rem; color: var(--text3); }
  .ev-time { font-size: 0.7rem; color: var(--text3); font-family: var(--mono); }
  .ev-title { font-size: 0.88rem; font-weight: 500; color: var(--text); margin-bottom: 0.3rem; }
  .ev-expl { font-size: 0.83rem; color: var(--text2); line-height: 1.65; }

  /* Risks */
  .risk-list { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .risk-pill {
    background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.2);
    color: #fca5a5; border-radius: 20px; padding: 0.3rem 0.9rem;
    font-size: 0.8rem;
  }

  /* Price triggers */
  .trigger-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .trig-card {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 10px; padding: 0.9rem 1.1rem;
  }
  .trig-card .tk { font-size: 0.68rem; color: var(--text3); text-transform: uppercase; letter-spacing: 0.07em; margin-bottom: 0.3rem; }
  .trig-card .tv { font-family: var(--mono); font-size: 1.1rem; font-weight: 600; color: var(--accent); }

  /* JSON toggle */
  .json-toggle-btn {
    background: none; border: 1px solid var(--border);
    color: var(--text2); font-family: var(--font); font-size: 0.82rem;
    padding: 0.45rem 1rem; border-radius: 6px; cursor: pointer;
    transition: all 0.15s; margin-bottom: 0.8rem;
  }
  .json-toggle-btn:hover { border-color: var(--accent2); color: var(--text); }
  .json-box {
    display: none; background: #060d16;
    border: 1px solid var(--border); border-radius: 10px;
    padding: 1.2rem 1.4rem; overflow-x: auto;
    white-space: pre; font-family: var(--mono); font-size: 0.78rem;
    color: #94a3b8; max-height: 500px; overflow-y: auto;
  }
  .json-box.open { display: block; }

  /* Loading overlay */
  .loading-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(10,14,26,0.85); backdrop-filter: blur(4px);
    z-index: 100; align-items: center; justify-content: center;
    flex-direction: column; gap: 1.2rem;
  }
  .loading-overlay.show { display: flex; }
  .loading-ring {
    width: 56px; height: 56px;
    border: 3px solid var(--border);
    border-top: 3px solid var(--accent);
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
  }
  .loading-text { color: var(--text2); font-size: 0.9rem; }
  .loading-steps { display: flex; flex-direction: column; gap: 0.4rem; margin-top: 0.5rem; }
  .loading-step { font-size: 0.8rem; color: var(--text3); display: flex; align-items: center; gap: 0.5rem; }
  .loading-step.active { color: var(--accent); }
  .loading-step.done { color: var(--positive); }
  .step-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }

  @media (max-width: 768px) {
    .main { padding: 1.2rem 1rem 3rem; }
    .header { padding: 1.5rem 1.2rem; }
    .form-grid { grid-template-columns: 1fr; }
    .provider-grid { grid-template-columns: repeat(2, 1fr); }
    .decision-strip { grid-template-columns: 1fr; }
    .prob-numbers { gap: 1.5rem; }
    .pv { font-size: 2rem !important; }
    .trigger-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="loading-overlay" id="loadingOverlay">
  <div class="loading-ring"></div>
  <div>
    <div class="loading-text" style="text-align:center;margin-bottom:1rem;">正在分析市场数据…</div>
    <div class="loading-steps">
      <div class="loading-step" id="step1"><span class="step-dot"></span>获取 Polymarket 市场数据</div>
      <div class="loading-step" id="step2"><span class="step-dot"></span>拉取订单簿 &amp; 历史价格</div>
      <div class="loading-step" id="step3"><span class="step-dot"></span>获取交易记录</div>
      <div class="loading-step" id="step4"><span class="step-dot"></span>获取相关新闻</div>
      <div class="loading-step" id="step5"><span class="step-dot"></span>AI 深度分析中…</div>
    </div>
  </div>
</div>

<div class="header">
  <div class="header-inner">
    <div class="logo-row">
      <div class="logo-mark">P∞</div>
      <div class="logo-text">Poly<span>Predict</span></div>
    </div>
    <div class="header-desc">
      输入你的 AI API 密钥、新闻令牌和 Polymarket 事件，系统将融合订单簿、历史价格、成交记录与新闻情报，
      量化评估 Yes 价格触及目标阈值的概率。
    </div>
  </div>
</div>

<div class="main">

  <!-- Config Panel -->
  <div class="config-panel">
    <div class="panel-title">配置参数</div>

    <div class="form-grid">

      <!-- Provider selection -->
      <div class="field full">
        <label>AI 提供商</label>
        <div class="provider-grid" id="providerGrid">
          <button class="provider-btn active" data-provider="deepseek" onclick="selectProvider(this)">
            <span class="provider-icon">🐋</span>DeepSeek
          </button>
          <button class="provider-btn" data-provider="claude" onclick="selectProvider(this)">
            <span class="provider-icon">🤖</span>Claude
          </button>
          <button class="provider-btn" data-provider="chatgpt" onclick="selectProvider(this)">
            <span class="provider-icon">💬</span>ChatGPT
          </button>
          <button class="provider-btn" data-provider="doubao" onclick="selectProvider(this)">
            <span class="provider-icon">🫘</span>豆包
          </button>
        </div>
      </div>

      <div class="field">
        <label>AI API Key</label>
        <input type="password" id="aiKey" placeholder="sk-..." autocomplete="off" />
        <span class="field-hint" id="keyHint">DeepSeek API Key（deepseek.com）</span>
      </div>

      <div class="field" id="modelField">
        <label>模型 <span style="color:var(--text3);font-weight:400">(可选，留空用默认)</span></label>
        <input type="text" id="customModel" placeholder="deepseek-chat" />
        <span class="field-hint" id="modelHint">留空则使用默认模型</span>
      </div>

      <div class="field">
        <label>News API Token</label>
        <input type="password" id="newsToken" placeholder="nrk_..." autocomplete="off" />
        <span class="field-hint">news.ruilisi.com 令牌</span>
      </div>

      <div class="field">
        <label>新闻查询词 <span style="color:var(--text3);font-weight:400">(可选)</span></label>
        <input type="text" id="newsQuery" placeholder="Iran nuclear uranium" value="Iran nuclear uranium" />
        <span class="field-hint">留空则用主题默认词</span>
      </div>

      <div class="field full">
        <label>Polymarket 事件（URL 或 Slug）</label>
        <input type="text" id="eventInput" placeholder="https://polymarket.com/event/iran-agrees-to-end-enrichment... 或直接填 slug" />
      </div>

      <div class="field">
        <label>目标 Yes 价格（Threshold）</label>
        <input type="number" id="targetPrice" value="0.60" step="0.01" min="0.01" max="0.99" />
        <span class="field-hint">例如 0.60 表示预测 Yes 涨到 60¢ 的概率</span>
      </div>

      <div class="field">
        <label>新闻时间窗口</label>
        <select id="newsWindow">
          <option value="7d">最近 7 天</option>
          <option value="14d">最近 14 天</option>
          <option value="30d" selected>最近 30 天</option>
          <option value="60d">最近 60 天</option>
        </select>
      </div>

      <button class="btn-analyze" id="analyzeBtn" onclick="runAnalysis()">
        <span class="spinner" id="spinner"></span>
        <span id="btnText">🚀 开始分析</span>
      </button>

    </div>
  </div>

  <div class="error-bar" id="errorBar"></div>

  <!-- Result -->
  <div id="result">

    <!-- Probability hero -->
    <div class="prob-hero">
      <div class="prob-label">P( Yes ≥ 目标价 ) 概率区间</div>
      <div class="prob-numbers">
        <div class="prob-item">
          <div class="pl">悲观估计</div>
          <div class="pv low" id="probLow">--%</div>
        </div>
        <div class="prob-divider"></div>
        <div class="prob-item">
          <div class="pl">中位数</div>
          <div class="pv mid" id="probMid">--%</div>
        </div>
        <div class="prob-divider"></div>
        <div class="prob-item">
          <div class="pl">乐观估计</div>
          <div class="pv high" id="probHigh">--%</div>
        </div>
      </div>
      <div class="prob-sentence" id="probSentence">等待分析…</div>
    </div>

    <!-- Decision -->
    <div class="decision-strip">
      <div class="dec-card">
        <div class="dk">建议操作</div>
        <div class="dv"><span class="action-tag" id="decAction">--</span></div>
      </div>
      <div class="dec-card">
        <div class="dk">理由</div>
        <div class="dv" id="decReason" style="font-size:0.84rem;color:var(--text2);">--</div>
      </div>
      <div class="dec-card">
        <div class="dk">入场计划</div>
        <div class="dv" id="decEntry" style="font-size:0.84rem;color:var(--text2);">--</div>
      </div>
    </div>

    <!-- Price triggers -->
    <div class="section">
      <div class="section-title"><span class="icon">📈</span> 价格触发条件</div>
      <div class="trigger-grid">
        <div class="trig-card">
          <div class="tk">考虑买入 Below</div>
          <div class="tv" id="trigBuy">--</div>
        </div>
        <div class="trig-card">
          <div class="tk">中性观望区间</div>
          <div class="tv" id="trigNeutral" style="font-size:0.9rem;">--</div>
        </div>
        <div class="trig-card">
          <div class="tk">避免追高 Above</div>
          <div class="tv" id="trigAvoid">--</div>
        </div>
      </div>
    </div>

    <!-- Detailed reasoning -->
    <div class="section">
      <div class="section-title"><span class="icon">🧠</span> 详细推理 <span class="cnt" id="reasoningLen"></span></div>
      <div class="reasoning-text" id="reasoningText"></div>
    </div>

    <!-- Market evidence -->
    <div class="section">
      <div class="section-title"><span class="icon">📊</span> 市场证据 <span class="cnt" id="marketCnt"></span></div>
      <div class="evidence-list" id="marketEvidence"></div>
    </div>

    <!-- News evidence -->
    <div class="section">
      <div class="section-title"><span class="icon">📰</span> 新闻证据 <span class="cnt" id="newsCnt"></span></div>
      <div class="evidence-list" id="newsEvidence"></div>
    </div>

    <!-- Key risks -->
    <div class="section">
      <div class="section-title"><span class="icon">⚠️</span> 关键风险</div>
      <div class="risk-list" id="riskList"></div>
    </div>

    <!-- JSON -->
    <div class="section">
      <button class="json-toggle-btn" id="jsonToggle" onclick="toggleJson()">📄 查看完整 JSON</button>
      <div class="json-box" id="jsonBox"></div>
    </div>

  </div>
</div>

<script>
  // ── Provider selection ──
  const PROVIDER_HINTS = {
    deepseek: { key: 'DeepSeek API Key（platform.deepseek.com）', model: 'deepseek-chat' },
    claude:   { key: 'Anthropic API Key（console.anthropic.com）', model: 'claude-sonnet-4-5' },
    chatgpt:  { key: 'OpenAI API Key（platform.openai.com）', model: 'gpt-4o' },
    doubao:   { key: '豆包 / 火山引擎 API Key', model: '你的 endpoint ID（ep-...）' }
  };

  let currentProvider = 'deepseek';

  function selectProvider(btn) {
    document.querySelectorAll('.provider-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentProvider = btn.dataset.provider;
    const h = PROVIDER_HINTS[currentProvider];
    document.getElementById('keyHint').textContent = h.key;
    document.getElementById('customModel').placeholder = h.model;
    document.getElementById('modelHint').textContent = `留空使用默认: ${h.model}`;
  }

  // ── FIXED: 前端 fetch 处理，先读 text() 再尝试 JSON.parse ──
  async function runAnalysis() {
    const aiKey     = document.getElementById('aiKey').value.trim();
    const newsToken = document.getElementById('newsToken').value.trim();
    const event     = document.getElementById('eventInput').value.trim();
    const target    = parseFloat(document.getElementById('targetPrice').value);
    const model     = document.getElementById('customModel').value.trim();
    const newsWin   = document.getElementById('newsWindow').value;
    const newsQ     = document.getElementById('newsQuery').value.trim();

    if (!aiKey || !newsToken || !event) {
      showError('请填写 AI API Key、News Token 和 Polymarket 事件。');
      return;
    }

    clearError();
    showLoading(true);
    document.getElementById('result').classList.remove('show');

    try {
      const res = await fetch('/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: currentProvider,
          ai_key: aiKey,
          custom_model: model || null,
          news_token: newsToken,
          event_slug: event,
          target_price: target,
          news_window: newsWin,
          news_query: newsQ || null
        })
      });

      // FIXED: 先读取文本，再尝试解析 JSON
      const rawText = await res.text();
      let data;
      try {
        data = JSON.parse(rawText);
      } catch (parseError) {
        // 如果返回的是 HTML 错误页或空内容，显示原始内容（截断）
        const preview = rawText ? (rawText.length > 500 ? rawText.slice(0, 500) + '…' : rawText) : '[空响应：通常是平台网关超时、后端进程崩溃或请求被代理截断]';
        throw new Error(`服务器返回非 JSON 内容。可能原因：网关超时或后端错误。\n原始内容预览：\n${preview}`);
      }

      if (!res.ok || data.error) {
        throw new Error(data.error || `请求失败 (HTTP ${res.status})`);
      }

      renderResult(data);
    } catch (err) {
      showError('❌ ' + err.message);
    } finally {
      showLoading(false);
    }
  }

  function renderResult(data) {
    const prob = data.probability_yes_reaches_target || {};
    document.getElementById('probLow').textContent  = fmt(prob.low)  + '%';
    document.getElementById('probMid').textContent  = fmt(prob.mid)  + '%';
    document.getElementById('probHigh').textContent = fmt(prob.high) + '%';
    document.getElementById('probSentence').textContent = data.final_answer_one_sentence || '';

    const dec = data.decision || {};
    const actionEl = document.getElementById('decAction');
    actionEl.textContent = dec.action || '--';
    actionEl.className = 'action-tag action-' + (dec.action || 'wait');
    document.getElementById('decReason').textContent = dec.reason || '--';
    document.getElementById('decEntry').textContent  = dec.entry_plan || '--';

    const trig = data.price_triggers || {};
    document.getElementById('trigBuy').textContent    = trig.consider_buy_below != null ? '$' + trig.consider_buy_below : '--';
    document.getElementById('trigNeutral').textContent = trig.neutral_zone || '--';
    document.getElementById('trigAvoid').textContent  = trig.avoid_chasing_above != null ? '$' + trig.avoid_chasing_above : '--';

    const reasoning = data.detailed_reasoning || '';
    document.getElementById('reasoningText').textContent = reasoning;
    document.getElementById('reasoningLen').textContent  = reasoning.length + ' 字';

    renderEvidence(data.market_evidence || [], 'marketEvidence', 'marketCnt', 'market');
    renderEvidence(data.news_evidence   || [], 'newsEvidence',  'newsCnt',   'news');

    const risks = data.key_risks || [];
    const riskEl = document.getElementById('riskList');
    riskEl.innerHTML = risks.length
      ? risks.map(r => `<span class="risk-pill">${r}</span>`).join('')
      : '<span style="color:var(--text3);font-size:0.85rem">未识别到关键风险</span>';

    document.getElementById('jsonBox').textContent = JSON.stringify(data, null, 2);
    document.getElementById('result').classList.add('show');
    document.getElementById('result').scrollIntoView({ behavior: 'smooth' });
  }

  function renderEvidence(items, containerId, cntId, type) {
    document.getElementById(cntId).textContent = items.length + ' 条';
    const el = document.getElementById(containerId);
    el.innerHTML = '';
    items.forEach((item, i) => {
      const impact = item.impact_on_yes_reaches_target || 'neutral';
      const cls = { positive:'positive', negative:'negative', neutral:'neutral' }[impact] || 'neutral';
      const card = document.createElement('div'); card.className = 'ev-card';
      if (type === 'news') {
        card.innerHTML = `
          <div class="ev-meta">
            <span class="ev-id">${item.news_id || ('N'+( i+1))}</span>
            <span class="ev-impact ${cls}">${impact}</span>
            <span class="ev-source">${item.source || ''}</span>
            <span class="ev-time">${item.published_at || ''}</span>
          </div>
          <div class="ev-title">${item.title || ''}</div>
          <div class="ev-expl">${item.explanation || ''}</div>`;
      } else {
        card.innerHTML = `
          <div class="ev-meta">
            <span class="ev-id">#${i+1}</span>
            <span class="ev-impact ${cls}">${impact}</span>
          </div>
          <div class="ev-title">${item.evidence || ''}</div>
          <div class="ev-expl">${item.explanation || ''}</div>`;
      }
      el.appendChild(card);
    });
  }

  function fmt(v) { return v != null ? v : '--'; }

  function showError(msg) {
    const el = document.getElementById('errorBar');
    el.textContent = msg; el.classList.add('show');
  }
  function clearError() { document.getElementById('errorBar').classList.remove('show'); }

  function showLoading(on) {
    document.getElementById('loadingOverlay').classList.toggle('show', on);
    document.getElementById('analyzeBtn').disabled = on;
  }

  function toggleJson() {
    const box = document.getElementById('jsonBox');
    box.classList.toggle('open');
    document.getElementById('jsonToggle').textContent =
      box.classList.contains('open') ? '📄 收起 JSON' : '📄 查看完整 JSON';
  }

  // Keyboard shortcut
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') runAnalysis();
  });
</script>
</body>
</html>"""

# ============================================================
# Flask 路由
# ============================================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/analyze', methods=['POST'])
def analyze():
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return json_error("请求体不是合法 JSON。请确认前端使用 Content-Type: application/json。", 400)

        provider     = data.get('provider', 'deepseek')
        ai_key       = data.get('ai_key')
        custom_model = data.get('custom_model')
        news_token   = data.get('news_token')
        raw_slug     = data.get('event_slug')
        news_window  = data.get('news_window', '30d')
        news_query   = data.get('news_query')

        try:
            target_price = float(data.get('target_price', 0.60))
        except (TypeError, ValueError):
            return json_error("target_price 必须是数字，例如 0.60。", 400)

        if not ai_key or not news_token or not raw_slug:
            return json_error("缺少必要参数：ai_key / news_token / event_slug", 400)
        if provider not in PROVIDER_CONFIG:
            return json_error(f"不支持的 provider：{provider}", 400)

        event_slug = extract_slug(raw_slug)
        if not event_slug:
            return json_error("无法从输入中提取 Polymarket slug。", 400)

        # 1. Polymarket 数据
        pm_data = get_polymarket_data(event_slug)
        yes_token    = pm_data.get("yes_token_id")
        condition_id = pm_data.get("condition_id")
        if not yes_token:
            return json_error("没有从 Polymarket 市场数据中取到 yes_token_id。请确认 slug 是具体 market slug。", 502)
        if pm_data.get("yes_price") is None:
            return json_error("没有从 Polymarket 市场数据中取到 Yes 价格。", 502)

        # 2. 订单簿
        orderbook = get_orderbook(yes_token)
        orderbook_summary = summarize_orderbook(
            orderbook.get("bids", []), orderbook.get("asks", []), target_price
        )

        # 3. 历史价格：失败不终止主流程
        try:
            price_history = get_price_history(yes_token)
            price_history_summary = summarize_price_history(price_history, target_price)
        except Exception as e:
            price_history_summary = {"count": 0, "latest_price": None, "ever_reached_target": False, "error": str(e)}

        # 4. 交易记录：失败不终止主流程
        try:
            trade_summary = summarize_trades(get_trade_history(condition_id, limit=500)) if condition_id else {"count": 0, "error": "missing condition_id"}
        except Exception as e:
            trade_summary = {"count": 0, "error": str(e)}

        # 5. 新闻：失败不终止主流程
        try:
            news = get_news(news_token, window=news_window, q=news_query or "Iran nuclear uranium")
            news_text = format_news_for_prompt(news)
        except Exception as e:
            news_text = f"新闻获取失败：{e}"

        # 6. 构建 Prompt
        messages = build_prompts(
            pm_data, orderbook_summary, price_history_summary,
            trade_summary, news_text, target_price
        )

        # 7. 调用 AI
        result = call_ai(provider, ai_key, messages, custom_model=custom_model)
        return jsonify(result)

    except requests.exceptions.Timeout as e:
        return json_error("外部接口请求超时。建议缩短新闻窗口、减少新闻数量，或稍后重试。", 504, e)
    except requests.exceptions.HTTPError as e:
        body = ""
        if getattr(e, "response", None) is not None:
            body = e.response.text[:1500]
        return json_error(f"外部接口 HTTP 错误：{e}", 502, body)
    except requests.exceptions.RequestException as e:
        return json_error("外部接口网络请求失败。", 502, e)
    except Exception as e:
        app.logger.exception("/analyze failed")
        return json_error(str(e), 500, traceback.format_exc())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
