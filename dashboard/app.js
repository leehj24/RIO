// UI Elements
const out = document.getElementById("out");
let priceChartInstance = null; // Chart.js instance holder

// Helper to format currency
function formatKRW(val) {
  if (val === null || val === undefined || isNaN(val)) return "₩0";
  const num = Math.round(parseFloat(val));
  return "₩" + num.toLocaleString('ko-KR');
}

function formatUSD(val) {
  if (val === null || val === undefined || isNaN(val)) return "$0.00";
  const num = parseFloat(val);
  return "$" + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Helper to format percentages
function formatPercent(val) {
  if (val === null || val === undefined || isNaN(val)) return "0.00%";
  const num = parseFloat(val);
  const sign = num > 0 ? "+" : "";
  return sign + num.toFixed(2) + "%";
}

// Print raw JSON to output console
function show(x) {
  out.textContent = JSON.stringify(x, null, 2);
}

// Base GET function
async function getJson(url, options) {
  try {
    const r = await fetch(url, options);
    const j = await r.json();
    show(j);
    return j;
  } catch (err) {
    show({ error: "Client-side fetch error", message: err.message });
    return null;
  }
}

// ------------------------------------------------------------
// Tab 1: Bot Control Renderers
// ------------------------------------------------------------

async function getBotStatus() {
  const data = await getJson("/api/bot/status");
  if (!data) return;

  const isDry = data.dry_run;
  const modeEl = document.getElementById("bot-mode-display");
  if (isDry) {
    modeEl.textContent = "DRY RUN";
    modeEl.style.color = "var(--warning)";
  } else {
    modeEl.textContent = "LIVE ACTIVE";
    modeEl.style.color = "var(--success)";
  }

  document.getElementById("bot-loop-display").textContent = data.loop_seconds + " 초";
  
  // Update API limits if returned
  getApiUsage();
}

async function getApiUsage() {
  const data = await getJson("/api/bot/api-usage");
  if (!data || !data.providers) return;

  const providers = data.providers;
  
  if (providers.google) {
    const gLimit = providers.google.limit || 10;
    const gUsed = providers.google.used || 0;
    const gPct = Math.min(100, (gUsed / gLimit) * 100);
    document.getElementById("usage-google-bar").style.width = gPct + "%";
    document.getElementById("usage-google-txt").textContent = `${gUsed} / ${gLimit} calls`;
  }
  
  if (providers.nvidia) {
    const nLimit = providers.nvidia.limit || 10;
    const nUsed = providers.nvidia.used || 0;
    const nPct = Math.min(100, (nUsed / nLimit) * 100);
    document.getElementById("usage-nvidia-bar").style.width = nPct + "%";
    document.getElementById("usage-nvidia-txt").textContent = `${nUsed} / ${nLimit} calls`;
  }

  if (providers.gemini_api2) {
    const aLimit = providers.gemini_api2.limit || 30;
    const aUsed = providers.gemini_api2.used || 0;
    const aPct = Math.min(100, (aUsed / aLimit) * 100);
    document.getElementById("usage-gemini-api2-bar").style.width = aPct + "%";
    document.getElementById("usage-gemini-api2-txt").textContent = `${aUsed} / ${aLimit} calls`;
  }
}

async function getBotEvents() {
  const panel = document.getElementById("events-display-panel");
  if (panel) panel.style.display = "block";

  const tbody = document.getElementById("events-table-body");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;"><div class="loading-spinner"></div> 이벤트 데이터 로딩 중...</td></tr>`;

  try {
    // Ensure watchlistSymbols is loaded for translation
    if (!watchlistSymbols || !Array.isArray(watchlistSymbols) || watchlistSymbols.length === 0) {
      const data = await getJson("/api/bot/symbols");
      if (data && data.result) {
        const rawSymbols = data.result;
        watchlistSymbols = Array.isArray(rawSymbols) ? rawSymbols : Object.values(rawSymbols);
      } else {
        watchlistSymbols = [];
      }
    }

    const data = await getJson("/api/bot/events");
    if (!data) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-muted);">이벤트 데이터를 가져오지 못했습니다.</td></tr>`;
      return;
    }

    const events = data.result || [];
    if (events.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-muted);">평가 중인 활성 이벤트가 없습니다.</td></tr>`;
      return;
    }

    tbody.innerHTML = "";
    events.forEach(e => {
      const tr = document.createElement("tr");

      // Translate mapped_symbols to Korean names safely
      const mappedKoreanNames = e.mapped_symbols 
        ? e.mapped_symbols.map(sym => {
            if (sym === null || sym === undefined) return "N/A";
            const symStr = String(sym).trim();
            const found = watchlistSymbols.find(x => String(x.symbol).trim() === symStr);
            return found && found.name ? found.name : getCommonSymbolNameFallback(symStr);
          }).join(", ")
        : "N/A";

      tr.innerHTML = `
        <td><strong>${e.theme || "일반"}</strong></td>
        <td>${e.question || "질문 없음"}</td>
        <td><span class="badge badge-primary">${mappedKoreanNames}</span></td>
        <td>${e.b || "N/A"}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error("Error in getBotEvents:", err);
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--danger);">이벤트 목록 렌더링 중 오류 발생: ${err.message}</td></tr>`;
  }
}

// Fallback mappings for common US/KR tickers (safe string fallback)
function getCommonSymbolNameFallback(symbol) {
  if (symbol === null || symbol === undefined) return "N/A";
  const symStr = String(symbol).trim().toUpperCase();
  const map = {
    "AAPL": "애플",
    "NVDA": "엔비디아",
    "TSLA": "테슬라",
    "MSFT": "마이크로소프트",
    "AMZN": "아마존",
    "GOOGL": "구글",
    "GOOG": "구글",
    "META": "메타",
    "NFLX": "넷플릭스",
    "005930": "삼성전자",
    "000660": "SK하이닉스"
  };
  return map[symStr] || symStr;
}

async function getBotSymbols() {
  const data = await getJson("/api/bot/symbols");
  if (!data) return;

  const panel = document.getElementById("watchlist-display-panel");
  panel.style.display = "block";

  const tbody = document.getElementById("symbols-registry-body");
  tbody.innerHTML = "";

  const rawList = data.result || [];
  const list = Array.isArray(rawList) ? rawList : Object.values(rawList);

  if (list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;">등록된 종목이 없습니다.</td></tr>`;
    return;
  }

  list.forEach(item => {
    const tr = document.createElement("tr");
    const isTarget = item.tradable !== false;
    tr.innerHTML = `
      <td><strong>${item.symbol}</strong></td>
      <td>${item.name || "N/A"}</td>
      <td><span class="badge">${item.market || "KRX"}</span></td>
      <td><span class="badge ${isTarget ? 'badge-success' : 'badge-danger'}">${isTarget ? '적용' : '제외'}</span></td>
      <td><span style="font-size:11px; color:var(--text-muted);">${item.description || "-"}</span></td>
    `;
    tbody.appendChild(tr);
  });
}

function toggleJsonConsole() {
  const section = document.getElementById("json-console-section");
  if (section) section.style.display = section.style.display === "none" ? "block" : "none";
}

async function getRawWatchlist() {
  const panel = document.getElementById("watchlist-raw-panel");
  if (panel) panel.style.display = "block";
  const head = document.getElementById("watchlist-raw-head");
  const body = document.getElementById("watchlist-raw-body");
  body.innerHTML = `<tr><td colspan="9" style="text-align:center;"><div class="loading-spinner"></div> 로딩 중...</td></tr>`;

  const data = await getJson("/api/bot/watchlist-raw");
  const rows = (data && data.result) || [];
  if (rows.length === 0) {
    head.innerHTML = "";
    body.innerHTML = `<tr><td style="text-align:center; color: var(--text-muted);">관심종목 파일이 비어 있거나 읽지 못했어요 (data/watchlist_raw.csv)</td></tr>`;
    return;
  }
  const HEAD_KO = { symbol: "종목코드", name: "이름", raw_name: "원본이름", market: "시장", sector: "섹터", theme: "테마", enabled: "사용", resolved: "확인상태", note: "비고" };
  const cols = Object.keys(rows[0]);
  head.innerHTML = cols.map(c => `<th>${HEAD_KO[c] || c}</th>`).join("");
  body.innerHTML = rows.map(r => `<tr>${cols.map(c => `<td>${String(r[c] ?? "").slice(0, 60)}</td>`).join("")}</tr>`).join("");
}

// ------------------------------------------------------------
// 한글 매핑 (판단/사유)
// ------------------------------------------------------------
const ACTION_KO = {
  "BUY": "매수",
  "SELL": "매도",
  "HOLD": "관망",
  "SELL_SIGNAL_NO_POSITION": "매도신호(미보유)",
  "BUY_BLOCKED": "매수 차단",
  "BUY_SKIPPED": "매수 건너뜀",
  "DATA_SKIPPED": "데이터 부족으로 제외",
  "ORDER_RECONCILIATION_REQUIRED": "이전 주문 확인 필요",
  "BUYING_POWER_UNAVAILABLE": "매수가능금액 조회 실패",
};

const REASON_KO = {
  "model_action": "모델 판단",
  "exit_rules": "청산 규칙(손절/익절/트레일링)",
  "sell_signal_no_position": "매도 신호지만 보유 없음",
  "event_level_sell_not_symbol_specific": "이벤트 단위 매도 신호 (종목별 근거 없음 → 관망)",
  "technical_entry_not_confirmed": "기술적 진입 신호 미확인",
  "llm confidence too low": "LLM 신뢰도 부족",
  "too many api errors": "API 오류 과다",
  "daily loss limit reached": "일일 손실 한도 도달",
  "total drawdown limit reached": "누적 낙폭 한도 도달",
  "survival gate passed": "리스크 게이트 통과",
  "agent_did_not_select_this_symbol": "AI 에이전트가 이 종목을 선택하지 않음",
  "prior_order_reconciliation_required": "이전 주문 미정리",
  "max_new_buys_per_loop reached": "루프당 신규매수 한도 도달",
  "max_open_positions reached": "최대 보유종목 수 도달",
  "pending_broker_order_exists": "미체결 주문 존재",
  "market_closed_or_unconfirmed": "장 마감 또는 개장 미확인",
  "no_python_pre_screened_new_buy_candidate": "사전 스크리닝 통과 후보 없음",
};

function koAction(a) { return ACTION_KO[a] || a || "-"; }
function koReason(r) {
  if (!r) return "-";
  return REASON_KO[r] || REASON_KO[String(r).trim()] || r;
}

// ------------------------------------------------------------
// 자동투자 시작/정지 + 성과 요약
// ------------------------------------------------------------
async function setBotControl(run) {
  await getJson("/api/bot/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run: run }),
  });
  refreshBotSummary();
}

async function refreshBotSummary() {
  const data = await getJson("/api/bot/summary");
  if (!data) return;

  const stateEl = document.getElementById("bot-run-state");
  const modeEl = document.getElementById("bot-run-mode");
  const running = (data.control || {}).run !== false;
  stateEl.textContent = running ? "● 자동투자 작동 중" : "❚❚ 일시정지됨";
  stateEl.style.color = running ? "var(--success, #30a46c)" : "var(--warning, #f5a623)";
  modeEl.textContent = data.dry_run
    ? "(모의 모드: 실제 주문 없음)"
    : "(실거래 모드: 실제 주문 나감)";

  document.getElementById("sum-today-buys").textContent = (data.today_buys ?? 0) + "건";
  document.getElementById("sum-today-sells").textContent = (data.today_sells ?? 0) + "건";
  const perf = data.performance || {};
  document.getElementById("sum-daily-pnl").textContent = formatPercent((perf.daily_pnl_pct || 0) * 100);
  document.getElementById("sum-drawdown").textContent = formatPercent((perf.total_drawdown_pct || 0) * 100);
  document.getElementById("sum-equity").textContent =
    perf.portfolio_equity_krw != null ? formatKRW(perf.portfolio_equity_krw) : "-";

  const tbody = document.getElementById("executed-trades-body");
  const rows = data.executed_recent || [];
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; color: var(--text-muted);">아직 실제 체결된 주문이 없어요</td></tr>`;
  } else {
    tbody.innerHTML = rows.map(r => {
      const orderTxt = r.order && Object.keys(r.order).length
        ? `${r.order.quantity ?? "-"}주 / ${r.order.order_type ?? r.order.type ?? "-"}`
        : "-";
      return `<tr>
        <td>${(r.ts || "").replace("T", " ")}</td>
        <td>${r.name || r.symbol || "-"}</td>
        <td>${koAction(r.action)}</td>
        <td>${r.price != null ? Number(r.price).toLocaleString("ko-KR") : "-"}</td>
        <td>${orderTxt}</td>
        <td>${koReason(r.reason)}</td>
      </tr>`;
    }).join("");
  }
}

async function getBotTrades() {
  const panel = document.getElementById("trades-display-panel");
  if (panel) panel.style.display = "block";
  const tbody = document.getElementById("trades-table-body");
  if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;"><div class="loading-spinner"></div> 판단 로그 로딩 중...</td></tr>`;

  const data = await getJson("/api/bot/trades?n=50");
  if (!data || !data.result || !tbody) return;

  const rows = data.result.filter(r => r.symbol || r.action || r.error);
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color: var(--text-muted);">기록 없음</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.slice().reverse().map(r => {
    if (r.error) {
      return `<tr><td>${(r.ts || "").replace("T", " ")}</td><td colspan="6" style="color: var(--danger, #e5484d);">오류: ${String(r.error).slice(0, 120)}</td></tr>`;
    }
    const name = (r.symbol_info || {}).name || r.symbol || "-";
    return `<tr>
      <td>${(r.ts || "").replace("T", " ")}</td>
      <td>${name}</td>
      <td>${koAction(r.action)}</td>
      <td>${koReason(r.action_reason)}</td>
      <td>${r.llm_prob != null ? (r.llm_prob * 100).toFixed(1) + "%" : "-"}</td>
      <td>${r.confidence != null ? (r.confidence * 100).toFixed(0) + "%" : "-"}</td>
      <td>${r.price != null ? Number(r.price).toLocaleString("ko-KR") : "-"}</td>
    </tr>`;
  }).join("");
}

const PROVIDER_KO = {
  google: "구글 Gemini (증거 검색)",
  nvidia: "NVIDIA (확률 판단)",
  gemini_api2: "Gemini 에이전트팀",
  naver_search: "네이버 뉴스/블로그",
};

async function getLlmCalls() {
  const panel = document.getElementById("llm-calls-panel");
  if (panel) panel.style.display = "block";
  const body = document.getElementById("llm-calls-body");
  body.innerHTML = `<tr><td colspan="4" style="text-align:center;"><div class="loading-spinner"></div> 로딩 중...</td></tr>`;

  const data = await getJson("/api/bot/llm-calls?n=30");
  const rows = ((data && data.result) || []).filter(r => r.provider);
  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">LLM 호출 기록이 아직 없어요</td></tr>`;
    return;
  }
  body.innerHTML = rows.slice().reverse().map((r, i) => {
    const resp = r.response || {};
    const summary = resp._error
      ? `⚠ 오류: ${String(resp._error).slice(0, 80)}`
      : (resp.reason || resp.macro_summary || resp.recommendation || JSON.stringify(resp).slice(0, 80));
    const detailId = `llm-detail-${i}`;
    return `<tr style="cursor:pointer;" onclick="const d=document.getElementById('${detailId}'); d.style.display = d.style.display==='none' ? 'table-row' : 'none';">
        <td>${PROVIDER_KO[r.provider] || r.provider}</td>
        <td>${r.called_api ? "실제 호출" : "호출 안 함(예산/비활성)"}</td>
        <td>${String(r.question || r.prompt_type || "").slice(0, 50)}</td>
        <td>${String(summary).slice(0, 90)}</td>
      </tr>
      <tr id="${detailId}" style="display:none;"><td colspan="4"><pre style="white-space:pre-wrap; max-height:250px; overflow-y:auto; font-size:11px;">질문(프롬프트):\n${String(r.prompt || "").slice(0, 2000)}\n\n응답:\n${JSON.stringify(resp, null, 2).slice(0, 2000)}</pre></td></tr>`;
  }).join("");
}

async function getDataRequirements() {
  const panel = document.getElementById("data-req-panel");
  if (panel) panel.style.display = "block";
  const head = document.getElementById("data-req-head");
  const body = document.getElementById("data-req-body");
  body.innerHTML = `<tr><td colspan="9" style="text-align:center;"><div class="loading-spinner"></div> 로딩 중...</td></tr>`;

  const data = await getJson("/api/bot/data-requirements");
  const rows = (data && data.result) || [];
  if (rows.length === 0) {
    head.innerHTML = "";
    body.innerHTML = `<tr><td style="text-align:center; color: var(--text-muted);">재무 데이터 현황 파일이 아직 없어요 (봇이 한 루프 이상 돌면 생성돼요)</td></tr>`;
    return;
  }
  const cols = Object.keys(rows[0]);
  head.innerHTML = cols.map(c => `<th>${c}</th>`).join("");
  body.innerHTML = rows.map(r => `<tr>${cols.map(c => {
    const v = String(r[c] ?? "");
    const mark = v.toLowerCase() === "true" ? "✅" : v.toLowerCase() === "false" ? "❌" : v.slice(0, 50);
    return `<td>${mark}</td>`;
  }).join("")}</tr>`).join("");
}

async function getApiUsageDetail() {
  const panel = document.getElementById("api-usage-panel");
  if (panel) panel.style.display = "block";
  const body = document.getElementById("api-usage-body");
  body.innerHTML = `<tr><td colspan="4" style="text-align:center;"><div class="loading-spinner"></div> 로딩 중...</td></tr>`;

  const data = await getJson("/api/bot/api-usage");
  getApiUsage(); // 상단 사용량 바도 갱신
  const providers = (data && data.providers) || {};
  const rows = [];
  for (const [name, p] of Object.entries(providers)) {
    const calls = p.calls || [];
    if (calls.length === 0) {
      rows.push(`<tr><td>${PROVIDER_KO[name] || name}</td><td>${p.used ?? 0} / ${p.limit ?? "-"}</td><td colspan="2" style="color: var(--text-muted);">오늘 호출 없음</td></tr>`);
    } else {
      calls.slice(-20).reverse().forEach(c => {
        const meta = c.meta || {};
        rows.push(`<tr><td>${PROVIDER_KO[name] || name}</td><td>${p.used} / ${p.limit}</td><td>${(c.ts || "").replace("T", " ")}</td><td>${meta.prompt_type || meta.kind || ""} ${meta.event_id || meta.query || ""}</td></tr>`);
      });
    }
  }
  body.innerHTML = rows.join("") || `<tr><td colspan="4" style="text-align:center; color: var(--text-muted);">기록 없음</td></tr>`;
}

// ------------------------------------------------------------
// Tab 2: Asset & Portfolio Renderers
// ------------------------------------------------------------

async function getAccounts() {
  const data = await getJson("/api/accounts");
  if (!data || !data.result) return;

  const accountList = data.result;
  if (accountList.length > 0) {
    const act = accountList[0];
    
    // Header & Sidebar indicators
    document.getElementById("quick-account-no").textContent = `No: ${act.accountNo} (Seq: ${act.accountSeq})`;
    document.getElementById("portfolio-account-seq").textContent = `${act.accountType} (Seq: ${act.accountSeq})`;
    document.getElementById("portfolio-account-no").textContent = act.accountNo;
    
    // Automatically trigger buying power for resolved account
    getBuyingPower(act.accountSeq);
  }
}

async function getBuyingPower(accountSeq) {
  const base = accountSeq ? `/api/buying-power?account=${accountSeq}&` : "/api/buying-power?";
  const [krwData, usdData] = await Promise.all([
    getJson(`${base}currency=KRW`),
    getJson(`${base}currency=USD`),
  ]);

  if (krwData && krwData.result) {
    const formatted = formatKRW(krwData.result.cashBuyingPower || "0");
    document.getElementById("quick-buying-power").textContent = formatted;
    document.getElementById("portfolio-buying-power").textContent = formatted;
  }

  const usdVal = usdData && usdData.result ? usdData.result.cashBuyingPower : null;
  const usdFormatted = usdVal != null ? formatUSD(usdVal) : "조회 불가";
  const quickUsd = document.getElementById("quick-buying-power-usd");
  if (quickUsd) quickUsd.textContent = usdFormatted;
  const portfolioUsd = document.getElementById("portfolio-buying-power-usd");
  if (portfolioUsd) portfolioUsd.textContent = usdFormatted;
}

async function getPositions(profit = false) {
  const url = profit ? "/api/positions?profit=1" : "/api/positions";
  const data = await getJson(url);
  if (!data) return;

  const tbody = document.getElementById("positions-table-body");
  tbody.innerHTML = "";

  const items = data.result?.items || data.items || [];
  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 30px;">
      <i class="bi bi-box-open"></i> 현재 보유 중인 주식 잔고가 존재하지 않습니다.
    </td></tr>`;
    
    // Default zero values
    document.getElementById("portfolio-total-value").textContent = "₩0";
    document.getElementById("portfolio-total-pnl-badge").innerHTML = `<span class="badge badge-primary">손익률 0.00%</span>`;
    return;
  }

  let totalPurchase = 0;
  let totalEvaluation = 0;

  items.forEach(rawItem => {
    // Merge _estimated if available from the profit analysis mode
    const est = rawItem._estimated || {};
    const item = {
      symbol: rawItem.symbol,
      name: rawItem.name || est.symbol || rawItem.symbol,
      quantity: rawItem.quantity || est.quantity || 0,
      averagePrice: rawItem.averagePrice || est.averagePrice || 0,
      currentPrice: rawItem.currentPrice || est.currentPrice || 0,
      purchaseAmount: rawItem.purchaseAmount || est.purchaseAmount || 0,
      evaluationAmount: rawItem.evaluationAmount || est.evaluationAmount || 0,
      profitAndLoss: rawItem.profitAndLoss || est.profitAndLoss || 0,
      profitRatePercent: rawItem.profitRate || est.profitRatePercent || 0,
    };

    // Calculate fallbacks if estimation fields are missing
    if (!item.purchaseAmount && item.quantity && item.averagePrice) {
      item.purchaseAmount = parseFloat(item.quantity) * parseFloat(item.averagePrice);
    }
    if (!item.evaluationAmount && item.quantity && item.currentPrice) {
      item.evaluationAmount = parseFloat(item.quantity) * parseFloat(item.currentPrice);
    }
    if (!item.profitAndLoss) {
      item.profitAndLoss = parseFloat(item.evaluationAmount) - parseFloat(item.purchaseAmount);
    }
    if (!item.profitRatePercent && item.purchaseAmount) {
      item.profitRatePercent = (parseFloat(item.profitAndLoss) / parseFloat(item.purchaseAmount)) * 100;
    }

    totalPurchase += parseFloat(item.purchaseAmount);
    totalEvaluation += parseFloat(item.evaluationAmount);

    const isProfit = parseFloat(item.profitAndLoss) >= 0;
    const pnlClass = isProfit ? "text-success" : "text-danger";
    const pnlStyle = isProfit ? "color: var(--success);" : "color: var(--danger);";
    const badgeClass = isProfit ? "badge-success" : "badge-danger";

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${item.symbol}</strong></td>
      <td>${item.name}</td>
      <td>${parseFloat(item.quantity).toLocaleString()}</td>
      <td>${formatKRW(item.averagePrice)}</td>
      <td>${formatKRW(item.currentPrice)}</td>
      <td>${formatKRW(item.purchaseAmount)}</td>
      <td><strong>${formatKRW(item.evaluationAmount)}</strong></td>
      <td style="${pnlStyle}"><strong>${isProfit ? "+" : ""}${Math.round(item.profitAndLoss).toLocaleString()}</strong></td>
      <td><span class="badge ${badgeClass}">${formatPercent(item.profitRatePercent)}</span></td>
    `;
    tbody.appendChild(tr);
  });

  // Update Summary card
  const totalPnL = totalEvaluation - totalPurchase;
  const totalPnLRatio = totalPurchase > 0 ? (totalPnL / totalPurchase) * 100 : 0;
  
  document.getElementById("portfolio-total-value").textContent = formatKRW(totalEvaluation);
  
  const sumIsProfit = totalPnL >= 0;
  const sumBadgeClass = sumIsProfit ? "badge-success" : "badge-danger";
  document.getElementById("portfolio-total-pnl-badge").innerHTML = `
    <span class="badge ${sumBadgeClass}">평가 손익: ${sumIsProfit ? "+" : ""}${Math.round(totalPnL).toLocaleString()} (${formatPercent(totalPnLRatio)})</span>
  `;
}

// ------------------------------------------------------------
// Tab 3: Stock Analysis & Charts Renderers
// ------------------------------------------------------------

function getStocks() {
  const symbols = document.getElementById("symbols").value;
  getJson("/api/stocks?symbols=" + encodeURIComponent(symbols));
}

function getPrices() {
  const symbols = document.getElementById("symbols").value;
  getJson("/api/prices?symbols=" + encodeURIComponent(symbols));
}

// ------------------------------------------------------------
// Tab 4: Trading Desk Actions
// ------------------------------------------------------------

function issueToken() {
  getJson("/api/token");
}

function createOrder() {
  const body = {
    account_seq: null,
    symbol: document.getElementById("symbol").value,
    side: document.getElementById("side").value,
    order_type: document.getElementById("orderType").value,
    quantity: document.getElementById("quantity").value || null,
    order_amount: document.getElementById("orderAmount").value || null,
    price: document.getElementById("price").value || null,
    client_order_id: "dash-" + Date.now()
  };
  getJson("/api/order", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
}

function modifyOrder() {
  const body = {
    account_seq: null,
    order_id: document.getElementById("orderId").value,
    order_type: "LIMIT",
    quantity: document.getElementById("modifyQty").value || null,
    price: document.getElementById("modifyPrice").value || null
  };
  getJson("/api/modify", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
}

function cancelOrder() {
  const body = {
    account_seq: null,
    order_id: document.getElementById("orderId").value
  };
  getJson("/api/cancel", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
}

// The dashboard contains only live KR/US execution candidates.  Country tabs
// are generated from verified country-exposure ETFs and equities in symbols.csv.
let watchlistSymbols = [];
let executionSymbols = [];
let currentMarketFilter = 'ALL';
let currentSearchQuery = '';

// Active selected stock info for multi-interval chart switching
let activeStockSymbol = null;
let activeStockName = null;
let activeStockMarket = null;
let currentChartUnit = '1y'; // Default chart unit is 1 Year
let activeStockDescriptionFallbackVal = "";

// Helper to format currency dynamically based on currency type (KRW or USD)
function formatPrice(val, currency) {
  if (val === null || val === undefined || isNaN(val)) {
    return currency === "USD" ? "$0.00" : "₩0";
  }
  const num = parseFloat(val);
  if (currency === "USD" || currency === "$") {
    return "$" + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } else {
    return "₩" + Math.round(num).toLocaleString('ko-KR');
  }
}

// Load only symbols that can enter the Toss order path.  A temporary price
// API failure must not hide a configured execution candidate.
async function initStockWatchlist() {
  const stockListEl = document.getElementById("stock-list");
  if (!stockListEl) return;

  try {
    const executionData = await getJson("/api/bot/execution-symbols");
    if (!executionData || !executionData.result) {
      stockListEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding-top: 40px;">종목 목록을 불러오지 못했습니다.</div>`;
      return;
    }

    const rawExecution = executionData.result;
    let allSymbols = Array.isArray(rawExecution) ? rawExecution : Object.values(rawExecution);

    // Filter out rows without a valid ticker code
    allSymbols = allSymbols.filter(item => item.symbol && item.symbol.trim() !== "");
    executionSymbols = allSymbols;
    buildMarketTabs();

    // Do not prefetch every price on page load.  The sidebar must render the
    // complete verified order universe immediately; the selected symbol
    // fetches its own live price/chart below.
    watchlistSymbols = allSymbols;
    applySidebarFilters();
  } catch (err) {
    stockListEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding-top: 40px;">에러: ${err.message}</div>`;
  }
}

function countryTheme(item) {
  const match = String(item.theme || '').match(/^Country:\s*(.+)$/i);
  if (match) return match[1].trim();

  // A stock can keep its strategy theme (for example, TSM remains in
  // "AI Semiconductor") and still belong in a country tab.
  const exposure = String(item.note || '').match(/(?:^|;)country_exposure=([^;]+)/i);
  return exposure ? exposure[1].trim().replace(/_/g, " ") : "";
}

const countryLabels = {
  "Australia": "호주", "Brazil": "브라질", "Canada": "캐나다", "China": "중국",
  "France": "프랑스", "Germany": "독일", "India": "인도", "Indonesia": "인도네시아",
  "Italy": "이탈리아", "Japan": "일본", "Nigeria": "나이지리아", "Qatar": "카타르",
  "Saudi Arabia": "사우디", "Singapore": "싱가포르", "South Africa": "남아공",
  "Switzerland": "스위스", "Taiwan": "대만", "United Kingdom": "영국", "Vietnam": "베트남",
};

function countryLabel(country) {
  return countryLabels[country] || country;
}

function countryTabId(country) {
  return `market-tab-country-${country.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '').toLowerCase()}`;
}

// Country tabs deliberately hide their scrollbar. Pointer dragging keeps the
// complete market/category selector usable on desktop while native touch
// scrolling remains available on touch devices.
function enableHorizontalDragScroll(container) {
  if (!container || container.dataset.dragScrollBound === "true") return;
  container.dataset.dragScrollBound = "true";

  const state = {
    pointerId: null,
    startX: 0,
    startScrollLeft: 0,
    moved: false,
  };

  const finishDrag = event => {
    if (state.pointerId !== event.pointerId) return;
    if (container.hasPointerCapture?.(event.pointerId)) {
      container.releasePointerCapture(event.pointerId);
    }
    state.pointerId = null;
    container.classList.remove("is-dragging");
  };

  container.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    // Capturing a pointer that started on a button can retarget/suppress the
    // subsequent click in some browsers. Buttons always keep native click;
    // drag scrolling starts only from the tab strip's empty area.
    if (event.target.closest(".market-tab")) return;
    state.pointerId = event.pointerId;
    state.startX = event.clientX;
    state.startScrollLeft = container.scrollLeft;
    state.moved = false;
    container.setPointerCapture?.(event.pointerId);
    container.classList.add("is-dragging");
  });

  container.addEventListener("pointermove", event => {
    if (state.pointerId !== event.pointerId) return;
    const distance = event.clientX - state.startX;
    if (Math.abs(distance) > 4) state.moved = true;
    if (!state.moved) return;
    container.scrollLeft = state.startScrollLeft - distance;
    event.preventDefault();
  });

  container.addEventListener("pointerup", finishDrag);
  container.addEventListener("pointercancel", finishDrag);
}

function buildMarketTabs() {
  const container = document.getElementById("stock-market-tabs");
  if (!container) return;

  const countries = [...new Set(executionSymbols.map(countryTheme).filter(Boolean))]
    .sort((a, b) => countryLabel(a).localeCompare(countryLabel(b), 'ko'));
  const baseTabs = [
    { filter: 'ALL', label: '전체', id: 'market-tab-all' },
    { filter: 'KR', label: '국내', id: 'market-tab-kr' },
    { filter: 'US', label: '미국', id: 'market-tab-us' },
  ];
  const tabs = [
    ...baseTabs,
    ...countries.map(country => ({
      filter: `COUNTRY:${country}`,
      label: countryLabel(country),
      id: countryTabId(country),
    })),
  ];
  container.innerHTML = "";
  tabs.forEach(tab => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "market-tab";
    if (tab.filter === currentMarketFilter) button.classList.add("active");
    button.id = tab.id;
    button.textContent = tab.label;
    button.dataset.marketFilter = tab.filter;
    button.onclick = () => filterMarket(tab.filter);
    container.appendChild(button);
  });
  container.onwheel = event => {
    if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
      container.scrollLeft += event.deltaY;
      event.preventDefault();
    }
  };
  enableHorizontalDragScroll(container);
}

// Handle execution-market and country-exposure tab switches.
function filterMarket(market) {
  currentMarketFilter = market;
  document.querySelectorAll(".market-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.marketFilter === market);
  });
  
  applySidebarFilters();
}

// Handle search bar typing
function handleStockSearch() {
  const searchInput = document.getElementById("stock-search-input");
  if (searchInput) {
    currentSearchQuery = searchInput.value.trim().toLowerCase();
  }
  applySidebarFilters();
}

// Filter watchlist based on current filter & search query
function applySidebarFilters() {
  let filtered = executionSymbols;
  
  // 1. Filter by Market
  if (currentMarketFilter === 'KR') {
    filtered = filtered.filter(item => {
      const m = String(item.market || '').toUpperCase();
      return m === 'KR' || m.startsWith('KR_');
    });
  } else if (currentMarketFilter === 'US') {
    filtered = filtered.filter(item => {
      const m = String(item.market || '').toUpperCase();
      return m === 'US';
    });
  } else if (currentMarketFilter.startsWith('COUNTRY:')) {
    const country = currentMarketFilter.slice('COUNTRY:'.length);
    filtered = filtered.filter(item => countryTheme(item) === country);
  }
  
  // 2. Filter by Search Query
  if (currentSearchQuery) {
    filtered = filtered.filter(item => {
      const name = String(item.name || '').toLowerCase();
      const sym = String(item.symbol || '').toLowerCase();
      const theme = String(item.theme || '').toLowerCase();
      const sector = String(item.sector || '').toLowerCase();
      return name.includes(currentSearchQuery) || sym.includes(currentSearchQuery)
        || theme.includes(currentSearchQuery) || sector.includes(currentSearchQuery);
    });
  }
  
  renderStockSidebarList(filtered);
}

// Render the filtered stock list into the left sidebar
function renderStockSidebarList(symbols) {
  const stockListEl = document.getElementById("stock-list");
  if (!stockListEl) return;
  stockListEl.innerHTML = "";

  if (symbols.length === 0) {
    stockListEl.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding-top: 40px; font-size: 13px;">일치하는 종목이 없습니다.</div>`;
    return;
  }

  symbols.forEach(item => {
    const div = document.createElement("div");
    div.className = "stock-item";
    div.id = `sidebar-stock-${item.symbol}`;
    div.onclick = () => selectStock(item);

    const badgeHtml = `<span class="badge badge-success" style="font-size: 9px; padding: 2px 4px;">자동매매</span>`;

    const country = countryTheme(item);
    const marketLabel = country ? `${countryLabel(country)} 노출` : (item.market === 'US' ? '미국' : '국내');
    // Country products are actually ordered through Toss's US order route,
    // but the sidebar's purpose is to show the underlying investment market.
    // Showing `INDY | US` here was misleading because it looked like a US
    // stock instead of an India-exposure product.
    const symbolLabel = country
      ? `${item.symbol} | ${countryLabel(country)} 투자`
      : `${item.symbol} | ${item.market || "KR"}`;
    const marketBadgeHtml = `<span class="badge" style="font-size: 9px; padding: 2px 4px; background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text-muted);">${marketLabel}</span>`;

    div.innerHTML = `
      <div class="stock-item-info">
        <span class="stock-item-name">${item.name || item.symbol}</span>
        <span class="stock-item-code">${symbolLabel}</span>
      </div>
      <div style="display: flex; gap: 4px; align-items: center;">
        ${marketBadgeHtml}
        ${badgeHtml}
      </div>
    `;
    stockListEl.appendChild(div);
  });
}

// Select a stock and fetch its live details + draw chart
async function selectStock(item) {
  // Cache values for multi-interval chart switching
  activeStockSymbol = item.symbol;
  activeStockName = item.name || item.symbol;
  activeStockMarket = item.market;

  // 1. Highlight sidebar item
  document.querySelectorAll(".stock-item").forEach(el => el.classList.remove("active"));
  const selectedEl = document.getElementById(`sidebar-stock-${item.symbol}`);
  if (selectedEl) selectedEl.classList.add("active");

  // 2. Show detail container & hide placeholder
  document.getElementById("stock-detail-placeholder").style.display = "none";
  document.getElementById("stock-detail-card").style.display = "flex";

  // Show unit selector tabs and highlight the current active unit button
  const selectorEl = document.getElementById("chart-unit-selector");
  if (selectorEl) {
    selectorEl.style.display = "flex";
    document.querySelectorAll(".unit-tab").forEach(btn => btn.classList.remove("active"));
    const activeBtn = document.getElementById(`unit-tab-${currentChartUnit}`);
    if (activeBtn) {
      activeBtn.classList.add("active");
    }
  }

  // 3. Populate static values
  document.getElementById("active-stock-title").textContent = `${item.name || item.symbol} (${item.symbol})`;
  document.getElementById("detail-stock-code").textContent = item.symbol;
  document.getElementById("detail-stock-name").textContent = item.name || "-";
  const country = countryTheme(item);
  document.getElementById("detail-stock-market").textContent = country
    ? `${countryLabel(country)} 투자 대상`
    : (item.market || "KR");
  
  const tradableEl = document.getElementById("detail-stock-tradable");
  tradableEl.innerHTML = `<span class="badge badge-success">자동매매 적용 대상</span>`;

  activeStockDescriptionFallbackVal = item.description || "종목에 대한 관리자/전략 분석 코멘트가 등록되어 있지 않습니다.";
  loadAiAnalysisForActiveStock(false);

  // 4. Fetch live price
  document.getElementById("detail-stock-price").textContent = "조회 중...";
  document.getElementById("detail-stock-change").textContent = "--";
  const changeCard = document.getElementById("detail-stock-change-card");
  changeCard.style.background = "rgba(255, 255, 255, 0.02)";
  changeCard.style.borderColor = "var(--border)";

  const isUS = item.market && item.market.toUpperCase() !== 'KR' && !item.market.toUpperCase().startsWith('KR_');

  try {
    const priceData = await getJson(`/api/prices?symbols=${item.symbol}`);
    if (priceData && priceData.result && priceData.result.length > 0) {
      const pInfo = priceData.result[0];
      const curPrice = parseFloat(pInfo.lastPrice || pInfo.closePrice || pInfo.currentPrice || 0);
      const currency = pInfo.currency || (isUS ? 'USD' : 'KRW');
      
      document.getElementById("detail-stock-price").textContent = formatPrice(curPrice, currency);

      // Calculate or read change rate
      const changeRate = parseFloat(pInfo.changeRate || 0);
      let displayRate = formatPercent(changeRate);
      if (Math.abs(changeRate) < 0.2 && changeRate !== 0) {
        displayRate = formatPercent(changeRate * 100);
      } else {
        displayRate = formatPercent(changeRate);
      }
      
      document.getElementById("detail-stock-change").textContent = displayRate;

      if (changeRate > 0) {
        changeCard.style.background = "rgba(0, 198, 137, 0.05)";
        changeCard.style.borderColor = "rgba(0, 198, 137, 0.15)";
        document.getElementById("detail-stock-change").style.color = "var(--success)";
      } else if (changeRate < 0) {
        changeCard.style.background = "rgba(255, 74, 107, 0.05)";
        changeCard.style.borderColor = "rgba(255, 74, 107, 0.15)";
        document.getElementById("detail-stock-change").style.color = "var(--danger)";
      } else {
        document.getElementById("detail-stock-change").style.color = "var(--text-main)";
      }
    } else {
      document.getElementById("detail-stock-price").textContent = isUS ? "시세 미지원 (USD)" : "시세 미지원 (KRW)";
    }
  } catch (err) {
    document.getElementById("detail-stock-price").textContent = "시세 조회 오류";
  }

  // 5. Load Chart
  loadChartForActiveStock();
}

// Menu action to change chart period / interval unit
function changeChartUnit(unit) {
  currentChartUnit = unit;

  // Update active class on unit tab buttons
  document.querySelectorAll(".unit-tab").forEach(btn => btn.classList.remove("active"));
  const activeBtn = document.getElementById(`unit-tab-${unit}`);
  if (activeBtn) {
    activeBtn.classList.add("active");
  }

  loadChartForActiveStock();
}

// Fetch and render the chart for the currently active stock based on the active unit
function loadChartForActiveStock() {
  if (!activeStockSymbol) return;
  loadChartForSymbol(activeStockSymbol, activeStockName, activeStockMarket, currentChartUnit);
}

function chartDateLabel(date, interval) {
  const yy = String(date.getFullYear()).slice(-2);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hour = String(date.getHours()).padStart(2, '0');
  const minute = String(date.getMinutes()).padStart(2, '0');
  if (interval === '1m') return `${hour}:${minute}`;
  if (interval === '15m' || interval === '1h') return `${month}/${day} ${hour}:${minute}`;
  if (interval === '1mo') return `${date.getFullYear()}.${month}`;
  if (interval === '1w') return `${yy}.${month}.${day}`;
  return `${month}/${day}`;
}

function chartHistoryStatus(meta) {
  const statusEl = document.getElementById("chart-history-status");
  if (!statusEl) return;
  if (!meta) {
    statusEl.textContent = "차트 이력 정보를 확인할 수 없습니다.";
    statusEl.classList.add("partial");
    return;
  }
  const from = meta.available_from_utc ? new Date(meta.available_from_utc).toLocaleDateString('ko-KR') : "-";
  const to = meta.available_to_utc ? new Date(meta.available_to_utc).toLocaleDateString('ko-KR') : "-";
  const intervalLabels = { '1m': '1분봉', '15m': '15분봉', '1h': '1시간봉', '1d': '일봉', '1w': '주봉', '1mo': '월봉' };
  const intervalLabel = intervalLabels[meta.display_interval] || meta.display_interval || "봉";
  statusEl.classList.toggle("partial", Boolean(meta.partial_history));
  statusEl.textContent = meta.partial_history
    ? `${meta.range_label} 요청 · 실제 제공 이력 ${from} ~ ${to} (${intervalLabel}) · 상장일 또는 데이터 제공 시작일 이전은 표시하지 않습니다.`
    : `${meta.range_label} · ${intervalLabel} · ${from} ~ ${to} · ${meta.display_bars || 0}개 봉`;
}

// Fetch a persistent range and render it. The backend selects 1m/1d raw
// history and derives 15m/weekly/monthly display bars as needed.
async function loadChartForSymbol(symbol, name, market, range = '1y') {
  const chartCanvas = document.getElementById('priceChart');
  if (chartCanvas) chartCanvas.style.display = 'block';
  const statusEl = document.getElementById("chart-history-status");
  if (statusEl) {
    statusEl.classList.remove("partial");
    statusEl.textContent = "저장된 시세를 확인하고 부족한 기간을 동기화하는 중입니다...";
  }
  const isUS = market && market.toUpperCase() !== 'KR' && !market.toUpperCase().startsWith('KR_');

  const data = await getJson(`/api/chart?symbol=${encodeURIComponent(symbol)}&range=${encodeURIComponent(range)}`);
  if (!data) return;
  chartHistoryStatus(data.meta);

  let rawCandles = [];
  if (data.result) {
    if (Array.isArray(data.result)) {
      rawCandles = data.result;
    } else if (data.result.candles && Array.isArray(data.result.candles)) {
      rawCandles = data.result.candles;
    }
  }

  if (rawCandles.length === 0) {
    if (priceChartInstance) {
      priceChartInstance.destroy();
      priceChartInstance = null;
    }
    if (statusEl) {
      statusEl.classList.add("partial");
      statusEl.textContent = data.message || "해당 기간에 표시할 수 있는 시세가 없습니다.";
    }
    return;
  }

  const candles = data.meta && data.meta.order === 'ascending'
    ? [...rawCandles]
    : [...rawCandles].reverse();
  const interval = (data.meta && data.meta.display_interval) || '1d';

  const labels = candles.map(c => {
    const d = new Date(c.timestamp);
    return chartDateLabel(d, interval);
  });
  
  const closePrices = candles.map(c => parseFloat(c.closePrice));

  const ctx = document.getElementById('priceChart').getContext('2d');
  
  if (priceChartInstance) {
    priceChartInstance.destroy();
  }

  const gradient = ctx.createLinearGradient(0, 0, 0, 300);
  // Theme styling: Green/Emerald for US stocks, Blue/Sapphire for KRX stocks
  const mainColor = isUS ? '#00C689' : '#3772FF';
  const startColor = isUS ? 'rgba(0, 198, 137, 0.45)' : 'rgba(55, 114, 255, 0.45)';
  const endColor = isUS ? 'rgba(0, 198, 137, 0.01)' : 'rgba(55, 114, 255, 0.01)';
  
  gradient.addColorStop(0, startColor);
  gradient.addColorStop(1, endColor);

  priceChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: `${name} (${symbol}) 종가 변동 추이 (${(data.meta && data.meta.range_label) || range})`,
        data: closePrices,
        borderColor: mainColor,
        borderWidth: 2,
        backgroundColor: gradient,
        fill: true,
        tension: 0.25,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: '#FFF',
        pointHoverBorderColor: mainColor,
        pointHoverBorderWidth: 2
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          labels: {
            color: '#8E9AA8',
            font: { family: 'Plus Jakarta Sans', weight: '600', size: 11 }
          }
        },
        tooltip: {
          backgroundColor: '#0F172A',
          titleColor: '#8E9AA8',
          bodyColor: '#FFF',
          titleFont: { family: 'Plus Jakarta Sans', weight: '700' },
          bodyFont: { family: 'Plus Jakarta Sans' },
          borderColor: 'rgba(255, 255, 255, 0.08)',
          borderWidth: 1,
          padding: 10,
          displayColors: false,
          callbacks: {
            label: function(context) {
              const val = context.raw;
              if (isUS) {
                return "종가: $" + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
              } else {
                return "종가: " + Math.round(val).toLocaleString('ko-KR') + "원";
              }
            }
          }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.03)', borderDash: [2, 2] },
          ticks: { color: '#8E9AA8', maxTicksLimit: 12, font: { size: 10 } }
        },
        y: {
          grid: { color: 'rgba(255, 255, 255, 0.03)', borderDash: [2, 2] },
          ticks: {
            color: '#8E9AA8',
            font: { size: 10 },
            callback: function(val) {
              if (isUS) {
                return "$" + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
              } else {
                return val.toLocaleString() + "원";
              }
            }
          }
        }
      }
    }
  });
}

// Load Gemini AI qualitative report for active stock
async function loadAiAnalysisForActiveStock(forceRefresh = false) {
  const descEl = document.getElementById("detail-stock-description");
  const btnContainer = document.getElementById("ai-analyze-btn-container");
  if (!descEl || !activeStockSymbol) return;

  const shouldGenerate = forceRefresh; 
  
  if (shouldGenerate) {
    descEl.innerHTML = `
      <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; gap: 16px; color: var(--primary);">
        <div class="loading-spinner" style="width: 24px; height: 24px;"></div>
        <div style="font-size: 13px; font-weight: 600; text-align: center; line-height: 1.5;">
          Gemini AI 실시간 정밀 분석 리포트를 생성하는 중입니다...<br>
          <span style="font-size: 11px; color: var(--text-muted); font-weight: normal; display: block; margin-top: 6px;">
            ※ 실시간 뉴스 검색(Google Grounding) 및 글로벌 매크로 분석에 약 5~10초 소요됩니다.
          </span>
        </div>
      </div>
    `;
    if (btnContainer) {
      btnContainer.innerHTML = `
        <span style="font-size: 11px; color: var(--text-muted); display: flex; align-items: center; gap: 4px;">
          <div class="loading-spinner"></div> 생성 중...
        </span>
      `;
    }
  } else {
    descEl.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: center; padding: 20px; gap: 8px; color: var(--text-muted); font-size: 12.5px;">
        <div class="loading-spinner"></div> AI 분석 리포트 확인 중...
      </div>
    `;
  }

  try {
    const url = `/api/stock/analyze?symbol=${activeStockSymbol}${shouldGenerate ? '&generate=1&refresh=1' : ''}`;
    const res = await getJson(url);

    if (res && res.result && !res.error) {
      const data = res.result;
      const isCached = res.cached;

      // Render beautiful premium AI report
      let riskBadgeStyle = "background: rgba(0, 198, 137, 0.1); color: var(--success); border: 1px solid rgba(0, 198, 137, 0.2);";
      if (data.risk_level === '매우위험') {
        riskBadgeStyle = "background: rgba(255, 74, 107, 0.1); color: var(--danger); border: 1px solid rgba(255, 74, 107, 0.2);";
      } else if (data.risk_level === '위험') {
        riskBadgeStyle = "background: rgba(255, 189, 46, 0.1); color: #FFBD2E; border: 1px solid rgba(255, 189, 46, 0.2);";
      } else if (data.risk_level === '안전') {
        riskBadgeStyle = "background: rgba(55, 114, 255, 0.1); color: var(--primary); border: 1px solid rgba(55, 114, 255, 0.2);";
      }

      const cacheTxt = isCached ? " (캐시된 분석 리포트)" : " (실시간 생성 완료)";

      descEl.innerHTML = `
        <div style="display: flex; flex-direction: column; gap: 16px;">
          <!-- Header Banner -->
          <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(55, 114, 255, 0.04); padding: 8px 12px; border-radius: 6px; border: 1px solid rgba(55, 114, 255, 0.08);">
            <span style="font-size: 11px; font-weight: 700; color: #60A5FA; display: flex; align-items: center; gap: 4px;">
              <i class="bi bi-cpu-fill"></i> GEMINI 2.5 REPORT${cacheTxt}
            </span>
            <span style="font-size: 10px; color: var(--text-muted); display: flex; align-items: center; gap: 4px;">
              <span style="width: 6px; height: 6px; background: var(--success); border-radius: 50%; display: inline-block;"></span> Google Grounding Active
            </span>
          </div>

          <!-- Section 1: Company Intro -->
          <div>
            <span style="font-size: 12px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; display: flex; align-items: center; gap: 6px; margin-bottom: 6px;">
              <i class="bi bi-building" style="color: var(--primary);"></i> 1. 종목 및 비즈니스 소개
            </span>
            <p style="margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-main); font-weight: 500;">${data.company_intro}</p>
          </div>

          <!-- Section 2: Macro Analysis -->
          <div style="border-top: 1px solid var(--border); padding-top: 12px;">
            <span style="font-size: 12px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; display: flex; align-items: center; gap: 6px; margin-bottom: 6px;">
              <i class="bi bi-globe-americas" style="color: #60A5FA;"></i> 2. 거시 경제 환경 분석 (금리, 미국 정책 등)
            </span>
            <p style="margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-main); font-weight: 500;">${data.macro_analysis}</p>
          </div>

          <!-- Section 3: Vision -->
          <div style="border-top: 1px solid var(--border); padding-top: 12px;">
            <span style="font-size: 12px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; display: flex; align-items: center; gap: 6px; margin-bottom: 6px;">
              <i class="bi bi-rocket-takeoff-fill" style="color: var(--success);"></i> 3. 미래 성장 비전 및 전략 방향
            </span>
            <p style="margin: 0; font-size: 13px; line-height: 1.6; color: var(--text-main); font-weight: 500;">${data.vision}</p>
          </div>

          <!-- Section 4: Risk Level Card -->
          <div style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <span style="font-size: 12px; font-weight: 700; color: var(--text-muted);">
                <i class="bi bi-shield-fill-exclamation" style="color: var(--warning);"></i> 4. 종합 투자 위험 등급
              </span>
              <span class="badge" style="font-size: 11px; font-weight: 800; padding: 4px 10px; border-radius: 4px; ${riskBadgeStyle}">${data.risk_level}</span>
            </div>
            <p style="margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text-muted); font-weight: 500;">${data.risk_reason}</p>
          </div>
        </div>
      `;

      // Render refresh button in header
      if (btnContainer) {
        btnContainer.innerHTML = `
          <button class="btn" style="padding: 4px 10px; font-size: 11px; border-radius: 4px; display: flex; align-items: center; gap: 4px; background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border); color: var(--text-muted);" onclick="loadAiAnalysisForActiveStock(true)">
            <i class="bi bi-arrow-clockwise"></i> AI 분석 갱신
          </button>
        `;
      }
    } else if (res && res.result === null) {
      // No cache exists yet. Show opt-in screen!
      descEl.innerHTML = `
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px 16px; text-align: center; gap: 12px; background: rgba(255, 255, 255, 0.01); border: 1px dashed var(--border); border-radius: 8px;">
          <div style="font-size: 24px;">🔮</div>
          <div style="display: flex; flex-direction: column; gap: 4px;">
            <h4 style="margin: 0; font-size: 13.5px; font-weight: 700; color: var(--text-main);">Gemini AI 실시간 종목 분석 보고서</h4>
            <p style="margin: 0; font-size: 12px; color: var(--text-muted); line-height: 1.5;">
              금리 전망, 글로벌 거시 정책, 기업 비전 등을 실시간 구글 검색(Google Grounding) 기반으로 쉽게 분석하여 리포트를 제공합니다.
            </p>
          </div>
          <button class="btn btn-success" style="padding: 8px 16px; font-size: 12px; border-radius: 6px; display: flex; align-items: center; gap: 6px; margin-top: 4px;" onclick="loadAiAnalysisForActiveStock(true)">
            <i class="bi bi-cpu-fill"></i> AI 실시간 정밀 분석 생성하기
          </button>
          <span style="font-size: 10px; color: var(--text-muted);">※ 일일 API 사용량 한도가 소모되며, 한 번 생성되면 서버에 자동 캐싱되어 다음부터 즉시 열립니다.</span>
          
          <div style="margin-top: 12px; width: 100%; border-top: 1px solid var(--border); padding-top: 12px; text-align: left;">
            <span style="font-size: 11px; font-weight: 700; color: var(--text-muted); display: block; margin-bottom: 4px;">기본 관리자 코멘트</span>
            <p style="margin: 0; font-size: 12.5px; color: var(--text-muted); line-height: 1.5;">${activeStockDescriptionFallbackVal}</p>
          </div>
        </div>
      `;
      if (btnContainer) {
        btnContainer.innerHTML = '';
      }
    } else {
      // Backend returned error
      const errMsg = res ? (res.message || res.error) : "API 호출 중 오류가 발생했습니다.";
      const isNoKey = res && res.error === 'no_api_key';

      if (isNoKey) {
        descEl.innerHTML = `
          <div style="padding: 16px; background: rgba(255, 74, 107, 0.03); border: 1px solid rgba(255, 74, 107, 0.15); border-radius: 8px; font-size: 13px;">
            <p style="margin: 0 0 8px 0; font-size: 13px; font-weight: 700; color: var(--danger); display: flex; align-items: center; gap: 6px;">
              <i class="bi bi-exclamation-triangle-fill"></i> 구글 제미나이 API 키가 필요합니다.
            </p>
            <p style="margin: 0 0 12px 0; font-size: 12px; line-height: 1.5; color: var(--text-muted);">
              실시간 뉴스 검색 및 분석 리포트 생성을 위해서는 <strong>구글 제미나이 API 키(GOOGLE_API_KEY)</strong>가 필요합니다. 프로젝트 루트 폴더의 <code>.env</code> 파일에 키를 추가해 주세요.
            </p>
            <div style="font-size: 12px; color: var(--text-muted); border-top: 1px solid var(--border); padding-top: 10px;">
              <strong style="display: block; margin-bottom: 4px; color: var(--text-main);">기본 관리자 코멘트:</strong>
              ${activeStockDescriptionFallbackVal}
            </div>
          </div>
        `;
      } else {
        descEl.innerHTML = `
          <div style="padding: 16px; background: rgba(255, 255, 255, 0.01); border: 1px dashed var(--border); border-radius: 8px; font-size: 13px; color: var(--text-muted);">
            <p style="margin: 0 0 8px 0; color: var(--warning); font-weight: 600;">⚠️ 리포트 조회 실패: ${errMsg}</p>
            <p style="margin: 0 0 12px 0;">기본 설명: ${activeStockDescriptionFallbackVal}</p>
            <button class="btn btn-primary" style="padding: 6px 12px; font-size: 11px; border-radius: 4px;" onclick="loadAiAnalysisForActiveStock(true)">재시도</button>
          </div>
        `;
      }

      if (btnContainer) {
        btnContainer.innerHTML = '';
      }
    }
  } catch (err) {
    console.error("Error in loadAiAnalysisForActiveStock:", err);
    descEl.innerHTML = `
      <div style="padding: 16px; background: rgba(255, 255, 255, 0.01); border: 1px dashed var(--border); border-radius: 8px; font-size: 13px; color: var(--text-muted);">
        <p style="margin: 0 0 8px 0; color: var(--danger); font-weight: 600;">⚠️ 네트워크 에러가 발생했습니다.</p>
        <p style="margin: 0; font-size: 12px;">기본 설명: ${activeStockDescriptionFallbackVal}</p>
        <button class="btn btn-primary" style="padding: 6px 12px; font-size: 11px; border-radius: 4px;" onclick="loadAiAnalysisForActiveStock(true)">재시도</button>
      </div>
    `;
    if (btnContainer) {
      btnContainer.innerHTML = '';
    }
  }
}
