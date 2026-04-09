// ── State ────────────────────────────────────────────────────────────────────
let pairsData   = {};
let activeTrades = [];          // Live list from /api/trades
let activePair  = '';
let botRunning  = false;
let globalStats = { trades: 0, wins: 0, losses: 0, balance: 0, leverage: 0, currency: 'USD' };

let scanInterval  = null;
let tradesInterval = null;

// ── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Always fetch on page load – no gate on botRunning
    fetchAccountData();
    fetchActiveTrades();
    fetchPairsData().then(() => {
        if (Object.keys(pairsData).length > 0) activePair = Object.keys(pairsData)[0];
        initUI();
    });

    // Passive background polls (even when bot toggle is OFF)
    setInterval(fetchAccountData,   10000);  // account every 10 s
    setInterval(fetchActiveTrades,   3000);  // trades  every  3 s  (fast for live PnL)
    setInterval(fetchPairsData,     15000);  // zones / AI info every 15 s

    document.getElementById('masterToggle').addEventListener('click', toggleBot);
});

function initUI() {
    renderPairsList();
    if (pairsData[activePair]) loadPairDetail(activePair);
    updateGlobalStats();
}

// ── API Calls ─────────────────────────────────────────────────────────────────

async function fetchAccountData() {
    try {
        const res  = await fetch('/api/account');
        const data = await res.json();
        if (!data.error) {
            globalStats.trades   = data.trades;
            globalStats.wins     = data.wins;
            globalStats.losses   = data.losses;
            globalStats.balance  = data.balance;
            globalStats.leverage = data.leverage;
            globalStats.currency = data.currency;
            updateGlobalStats();
        }
    } catch (e) { console.error('Account Fetch Error:', e); }
}

/** Dedicated fast poll for open positions – hits /api/trades */
async function fetchActiveTrades() {
    try {
        const res    = await fetch('/api/trades');
        const trades = await res.json();
        if (!Array.isArray(trades)) return;

        activeTrades = trades;

        // Reflect trade data back into pairsData so renderTrade works
        // First clear all pair.trade fields
        for (let k in pairsData) pairsData[k].trade = null;
        // Then stamp each live trade onto its symbol
        for (const t of trades) {
            if (pairsData[t.symbol]) pairsData[t.symbol].trade = t;
        }

        // Re-render the trade card and pair list badges
        renderTradeCard();
        renderPairsList();
    } catch (e) { console.error('Trades Fetch Error:', e); }
}

async function fetchPairsData() {
    try {
        const res  = await fetch('/api/pairs');
        const data = await res.json();
        if (data.error) return;

        for (let key in data) {
            if (!pairsData[key]) {
                pairsData[key] = data[key];
            } else {
                // Only update non-trade fields here – trade comes from /api/trades
                pairsData[key].price      = data[key].price;
                pairsData[key].supply     = data[key].supply;
                pairsData[key].demand     = data[key].demand;
                pairsData[key].liquidity  = data[key].liquidity;
                pairsData[key].ai_summary = data[key].ai_summary;
                pairsData[key].logs       = data[key].logs;

                // Trade-open notification
                const hadTrade  = !!pairsData[key].trade;
                const hasTrade  = !!data[key].trade;
                if (!hadTrade && hasTrade) {
                    pairsData[key].logs.push({
                        time: new Date().toTimeString().split(' ')[0],
                        msg:  `▶ Live Trade Executed via MT5: ${data[key].trade.type} @ ${data[key].trade.entry}`,
                        type: 'success'
                    });
                }
            }
        }

        renderPairsList();
        if (pairsData[activePair]) loadPairDetail(activePair);
    } catch (e) { console.error('Pairs Fetch Error:', e); }
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function renderPairsList() {
    const list = document.getElementById('pairsList');
    list.innerHTML = '';
    for (let key in pairsData) {
        const pair = pairsData[key];
        const li   = document.createElement('li');
        if (key === activePair) li.className = 'active';

        // Live PnL badge if this pair has an open trade
        let badgeHtml = '';
        if (pair.trade) {
            const pnl      = pair.trade.pnl;
            const pnlClass = pnl >= 0 ? 'win' : 'loss';
            const pnlSign  = pnl >= 0 ? '+' : '';
            badgeHtml = `<span class="trade-badge ${pnlClass}">${pnlSign}$${pnl.toFixed(2)}</span>`;
        }

        li.innerHTML = `
            <span class="pair-name">${pair.name}</span>
            <span class="pair-price">${pair.price}</span>
            ${badgeHtml}`;
        li.onclick = () => { activePair = key; renderPairsList(); loadPairDetail(key); };
        list.appendChild(li);
    }
}

function renderZones(zonesArray, elementId) {
    const el = document.getElementById(elementId);
    if (!Array.isArray(zonesArray) || zonesArray.length === 0) {
        el.innerHTML = '<span class="no-trade">No active zones across timeframes</span>';
        return;
    }
    let html = '';
    zonesArray.forEach(z => {
        const nodesHtml = z.nodes.map(n => `<span class="z-range">${n}</span>`).join('');
        html += `<div class="z-row"><span class="z-tf">${z.tf}</span> ${nodesHtml}</div>`;
    });
    el.innerHTML = html;
}

function loadPairDetail(key) {
    if (!pairsData[key]) return;
    const pair = pairsData[key];

    // Header
    document.getElementById('activePairTitle').textContent = pair.name;
    const statusEl = document.getElementById('activePairStatus');
    statusEl.textContent = botRunning ? 'Syncing Live' : 'Connected';
    statusEl.className   = botRunning ? 'pair-status scanning' : 'pair-status';

    // Zones
    renderZones(pair.supply, 'zSupply');
    renderZones(pair.demand, 'zDemand');
    document.getElementById('zLiquidity').textContent = pair.liquidity || '--';

    // AI Summary
    if (pair.ai_summary) {
        let biasColor = '#FFF';
        if (pair.ai_summary.bias === 'BULLISH') biasColor = 'var(--up-color)';
        if (pair.ai_summary.bias === 'BEARISH') biasColor = 'var(--down-color)';
        document.getElementById('zAiSummary').innerHTML = `
            <div>Bias: <strong style="color:${biasColor}">${pair.ai_summary.bias}</strong></div>
            <div>Direction: <strong>${pair.ai_summary.direction}</strong></div>
            <div>Confidence: <strong>${pair.ai_summary.confidence}%</strong></div>`;
    }

    // AI Logs
    const logContainer = document.getElementById('aiLog');
    logContainer.innerHTML = '';
    (pair.logs || []).forEach(log => appendLogHTML(log, logContainer));

    // Trade card is driven globally by renderTradeCard()
    renderTradeCard();
}

/** Render the Active Trade Status card from the live activeTrades array */
function renderTradeCard() {
    const card = document.getElementById('tradeCard');

    if (!activeTrades || activeTrades.length === 0) {
        card.innerHTML = '<div class="no-trade" style="margin-top:10px;">No active trades running on MT5.</div>';
        return;
    }

    let html = '';
    for (const trade of activeTrades) {
        const pnlClass  = trade.pnl >= 0 ? 'win' : 'loss';
        const pnlSign   = trade.pnl >= 0 ? '+' : '';
        const typeClass = trade.type === 'BUY' ? 'buy' : 'sell';

        html += `
        <div class="trade-block" style="margin-bottom:1rem; border:1px solid rgba(255,255,255,0.08); border-radius:6px; padding:0.6rem;">
            <div style="margin-bottom:0.5rem; font-size:0.73rem; color:#fff;
                        background:rgba(255,255,255,0.1); padding:0.15rem 0.45rem;
                        display:inline-block; border-radius:3px; font-weight:700;">
                ${trade.symbol}
            </div>
            <div class="trade-grid">
                <div class="t-data"><span>Type</span>        <strong class="${typeClass}">${trade.type}</strong></div>
                <div class="t-data"><span>Entry</span>       <strong>${trade.entry}</strong></div>
                <div class="t-data"><span>Current</span>     <strong>${trade.current}</strong></div>
                <div class="t-data"><span>Stop Loss</span>   <strong class="loss">${trade.sl}</strong></div>
                <div class="t-data"><span>Take Profit</span> <strong class="win">${trade.tp}</strong></div>
                <div class="t-data"><span>Lots</span>        <strong>${trade.lots}</strong></div>
                <div class="t-data"><span>Live PnL</span>    <strong class="${pnlClass}">${pnlSign}$${trade.pnl.toFixed(2)}</strong></div>
                <div class="t-data"><span>Swap</span>        <strong>${trade.swap.toFixed(2)}</strong></div>
            </div>
        </div>`;
    }
    card.innerHTML = html;
}

// ── Log Helpers ───────────────────────────────────────────────────────────────

function appendLogHTML(log, container) {
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = `<span class="log-time">${log.time}</span><span class="log-${log.type}">${log.msg}</span>`;
    if (container) {
        container.prepend(div);
        container.scrollTop = 0;
    }
}

function addLogToPair(key, msg, type = 'info') {
    const time   = new Date().toTimeString().split(' ')[0];
    const logObj = { time, msg, type };
    if (!pairsData[key]) return;
    pairsData[key].logs = pairsData[key].logs || [];
    pairsData[key].logs.push(logObj);
    if (pairsData[key].logs.length > 50) pairsData[key].logs.shift();
    if (activePair === key) appendLogHTML(logObj, document.getElementById('aiLog'));
}

// ── Global Stats ──────────────────────────────────────────────────────────────

function updateGlobalStats() {
    document.getElementById('totTrades').textContent  = globalStats.trades;
    document.getElementById('totWins').textContent    = globalStats.wins;
    document.getElementById('totLosses').textContent  = globalStats.losses;
    const wr = globalStats.trades > 0
        ? ((globalStats.wins / globalStats.trades) * 100).toFixed(1)
        : '0.0';
    document.getElementById('winRate').textContent    = wr + '%';
    document.getElementById('accBalance').textContent = `$${Number(globalStats.balance).toFixed(2)}`;
    document.getElementById('accLev').textContent     = `1:${globalStats.leverage}`;
}

// ── Bot Toggle ────────────────────────────────────────────────────────────────

async function toggleBot() {
    const btn  = document.getElementById('masterToggle');
    botRunning = !botRunning;

    if (botRunning) {
        btn.textContent = 'Stop AI Engine';
        btn.classList.replace('primary', 'stop');

        // Immediate refresh on activation
        await fetchAccountData();
        await fetchActiveTrades();
        await fetchPairsData();
        loadPairDetail(activePair);

        // Faster polling while bot is active
        scanInterval   = setInterval(fetchPairsData,    5000);
        tradesInterval = setInterval(fetchActiveTrades, 2000);
    } else {
        btn.textContent = 'Start AI Engine';
        btn.classList.replace('stop', 'primary');
        clearInterval(scanInterval);
        clearInterval(tradesInterval);
        scanInterval   = null;
        tradesInterval = null;

        const statusEl = document.getElementById('activePairStatus');
        statusEl.textContent = 'Idle';
        statusEl.className   = 'pair-status';

        for (let key in pairsData) {
            addLogToPair(key, 'Data sync paused.', 'warn');
        }
    }
}
