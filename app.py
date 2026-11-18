
import streamlit as st
import requests
import pandas as pd
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

APP_TITLE = "GROKSTOCKBOT V3.0"
DB_PATH = Path("grokstockbot.db")
DEX = "https://api.dexscreener.com"

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🟢",
    layout="wide"
)

# ---------------- DESIGN ----------------

st.markdown("""
<style>
:root {
    --bg:#080b0d;
    --panel:#11171b;
    --line:#27343a;
    --green:#22d38a;
    --muted:#8da0a8;
    --red:#ff5b68;
    --amber:#f2bd55;
}
.stApp {
    background:var(--bg);
    color:#e7f0f2;
}
[data-testid="stHeader"] {
    background:transparent;
}
.block-container {
    padding-top:1.2rem;
    max-width:1500px;
}
h1,h2,h3 {
    letter-spacing:-.03em;
}
h1 {color:#eafff5;}
.agent-card {
    background:#11181c;
    border:1px solid #29363c;
    border-radius:12px;
    padding:13px;
    min-height:125px;
}
.agent-title {
    font-weight:800;
    font-size:.83rem;
}
.agent-desc {
    color:#8da0a8;
    font-size:.75rem;
    margin-top:5px;
}
.flow {
    border-left:2px solid #234b3d;
    padding:5px 0 5px 12px;
    margin:5px 0;
}
div.stButton > button {
    border-radius:9px;
    border:1px solid #285442;
    background:#10251d;
    color:#dfffee;
}
[data-testid="stMetric"] {
    background:#11181c;
    border:1px solid #27343a;
    padding:12px;
    border-radius:10px;
}
</style>
""", unsafe_allow_html=True)


# ---------------- DATABASE ----------------

def db():
    con = sqlite3.connect(DB_PATH)

    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            symbol TEXT,
            address TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            notional REAL,
            reason TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            agent TEXT,
            message TEXT,
            level TEXT
        )
    """)

    con.commit()
    return con


def log_event(agent, message, level="INFO"):
    con = db()

    con.execute(
        """INSERT INTO events
        (ts,agent,message,level)
        VALUES(?,?,?,?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            agent,
            message,
            level
        )
    )

    con.commit()
    con.close()


# ---------------- SCANNER ----------------

def fetch_latest_solana_profiles():

    url = f"{DEX}/token-profiles/latest/v1"

    response = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "GrokStockBot/3.0"}
    )

    response.raise_for_status()

    data = response.json()

    return [
        item for item in data
        if item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ][:25]


def fetch_pairs_for_profiles(profiles):

    addresses = list(dict.fromkeys(
        item["tokenAddress"]
        for item in profiles
    ))[:30]

    if not addresses:
        return []

    url = (
        f"{DEX}/tokens/v1/solana/"
        + ",".join(addresses)
    )

    response = requests.get(
        url,
        timeout=20,
        headers={"User-Agent": "GrokStockBot/3.0"}
    )

    response.raise_for_status()

    data = response.json()

    return data if isinstance(data, list) else []


@st.cache_data(ttl=60, show_spinner=False)
def load_solana_market():

    profiles = fetch_latest_solana_profiles()
    pairs = fetch_pairs_for_profiles(profiles)

    best = {}

    for pair in pairs:

        token = pair.get("baseToken") or {}
        address = token.get("address")

        if not address:
            continue

        liquidity = (
            pair.get("liquidity") or {}
        ).get("usd") or 0

        previous = best.get(address)

        if previous is None:
            best[address] = pair

        elif liquidity > (
            (previous.get("liquidity") or {}).get("usd") or 0
        ):
            best[address] = pair

    rows = []

    for pair in best.values():

        token = pair.get("baseToken") or {}
        txns = pair.get("txns") or {}
        h24 = txns.get("h24") or {}
        volume = pair.get("volume") or {}
        changes = pair.get("priceChange") or {}
        liquidity = pair.get("liquidity") or {}

        created = pair.get("pairCreatedAt")
        age_hours = None

        if created:
            age_hours = max(
                0,
                (
                    datetime.now(timezone.utc).timestamp()
                    * 1000 - created
                ) / 3600000
            )

        rows.append({
            "symbol": token.get("symbol", "?"),
            "name": token.get("name", "Unknown"),
            "address": token.get("address", ""),
            "price": float(pair.get("priceUsd") or 0),
            "liquidity": float(liquidity.get("usd") or 0),
            "volume24": float(volume.get("h24") or 0),
            "buys24": int(h24.get("buys") or 0),
            "sells24": int(h24.get("sells") or 0),
            "change1h": float(changes.get("h1") or 0),
            "change24h": float(changes.get("h24") or 0),
            "age_hours": age_hours,
            "dex": pair.get("dexId", "unknown"),
            "url": pair.get("url", "")
        })

    return sorted(
        rows,
        key=lambda item: (
            item["liquidity"],
            item["volume24"]
        ),
        reverse=True
    )


# ---------------- RUG CHECK ----------------

def risk_screen(row, min_liquidity, min_volume):

    reasons = []

    if row["liquidity"] < min_liquidity:
        reasons.append("Liquidity below threshold")

    if row["volume24"] < min_volume:
        reasons.append("24h volume below threshold")

    if row["price"] <= 0:
        reasons.append("Invalid price")

    if row["buys24"] + row["sells24"] < 10:
        reasons.append("Low transaction count")

    if (
        row["age_hours"] is not None
        and row["age_hours"] < 1
    ):
        reasons.append("Pool younger than one hour")

    return len(reasons) == 0, reasons


# ---------------- LOGGER ----------------

def get_trades():

    con = db()

    data = pd.read_sql_query(
        "SELECT * FROM trades ORDER BY id DESC LIMIT 100",
        con
    )

    con.close()
    return data


def get_events():

    con = db()

    data = pd.read_sql_query(
        "SELECT * FROM events ORDER BY id DESC LIMIT 60",
        con
    )

    con.close()
    return data


# ---------------- EQUITY ----------------

def equity_stats(starting_cash):

    trades = get_trades()
    cash = starting_cash
    wins = 0
    losses = 0

    if not trades.empty:

        for _, trade in trades.sort_values("id").iterrows():

            if trade["side"] == "BUY":
                cash -= float(trade["notional"])

            elif trade["side"] == "SELL":
                cash += float(trade["notional"])

    return cash, trades, wins, losses


# ---------------- SESSION STATE ----------------

if "starting_cash" not in st.session_state:
    st.session_state.starting_cash = 100.0

if "paper_enabled" not in st.session_state:
    st.session_state.paper_enabled = True

if "halted" not in st.session_state:
    st.session_state.halted = False

if "market_rows" not in st.session_state:
    st.session_state.market_rows = []

if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = None


# ---------------- CONTROL PANEL ----------------

with st.sidebar:

    st.markdown("## 🟢 CONTROL")
    st.caption("SOLANA • PAPER MODE")

    st.session_state.starting_cash = st.number_input(
        "Starting paper balance ($)",
        min_value=10.0,
        max_value=100000.0,
        value=float(st.session_state.starting_cash),
        step=10.0
    )

    min_liquidity = st.number_input(
        "Minimum liquidity ($)",
        min_value=0,
        max_value=10000000,
        value=10000,
        step=1000
    )

    min_volume = st.number_input(
        "Minimum 24h volume ($)",
        min_value=0,
        max_value=100000000,
        value=5000,
        step=1000
    )

    risk_pct = st.slider(
        "Maximum position size (%)",
        1, 10, 3
    )

    max_daily_loss = st.slider(
        "Daily stop threshold (%)",
        1, 10, 2
    )

    st.divider()

    st.toggle(
        "Paper trading enabled",
        key="paper_enabled"
    )

    if st.button("⛔ STOP ALL", use_container_width=True):

        st.session_state.halted = True

        log_event(
            "EXECUTOR",
            "Emergency stop activated.",
            "STOP"
        )

    if st.button("Resume paper mode", use_container_width=True):

        st.session_state.halted = False

        log_event(
            "EXECUTOR",
            "Paper mode resumed."
        )

    st.caption("No wallet connection. No real transactions.")


# ---------------- HEADER ----------------

h1, h2 = st.columns([3, 1])

with h1:

    st.markdown("# 🟢 Grok Bot Terminal")

    st.caption(
        "V3.0 • Multi-agent Solana scanner "
        "• Every trade gets a reason"
    )

with h2:

    st.markdown(
        "<div style='text-align:right;padding-top:12px'>"
        "<span style='color:#22d38a'>● PAPER MODE</span><br>"
        "<small>LIVE DATA • SIMULATED ORDERS</small>"
        "</div>",
        unsafe_allow_html=True
    )


# ---------------- TOP STATISTICS ----------------

cash, trades, wins, losses = equity_stats(
    st.session_state.starting_cash
)

top = st.columns(4)

top[0].metric("PAPER CASH", f"${cash:,.2f}")
top[1].metric("TRADES", str(len(trades)))
top[2].metric("WINS", str(wins))
top[3].metric("LOSSES", str(losses))

st.markdown("---")


# ---------------- SIX AGENTS ----------------

st.markdown("### AGENTS · 6 MODULES")

agents = [
    (
        "▣ SCANNER",
        "Finds latest Solana token profiles and pools",
        "SCAN",
        "#22d38a"
    ),
    (
        "▲ RUG CHECK",
        "Liquidity, volume and age screening",
        "FILTER",
        "#f2bd55"
    ),
    (
        "◇ SIZER",
        "Calculates paper position size",
        "RISK",
        "#bd91ff"
    ),
    (
        "Ⅱ EXECUTOR",
        "Simulated orders and emergency stop",
        "PAPER",
        "#22d38a"
    ),
    (
        "▤ LOGGER",
        "Stores decisions and simulated orders",
        "WRITE",
        "#f2bd55"
    ),
    (
        "◉ EQUITY",
        "Tracks paper cash and order history",
        "UPDATE",
        "#76aaff"
    )
]

cols = st.columns(3)

for i, (name, desc, badge, color) in enumerate(agents):

    with cols[i % 3]:

        st.markdown(
            f"""
            <div class="agent-card">
                <div class="agent-title">
                    {name}
                    <span style="float:right;color:{color}">
                        ● {badge}
                    </span>
                </div>
                <div class="agent-desc">{desc}</div>
            </div>
            """,
            unsafe_allow_html=True
        )


# ---------------- LIVE TRADING FLOW ----------------

st.markdown("---")

left, right = st.columns([1.25, 1])

with left:

    st.markdown(
        "### LIVE FLOW · SCAN → CHECK → SIZE → EXEC → LOG"
    )

    if st.button(
        "🔄 SCAN LATEST SOLANA PROFILES",
        use_container_width=True,
        disabled=st.session_state.halted
    ):

        try:

            with st.spinner("Scanner querying DexScreener..."):

                st.session_state.market_rows = load_solana_market()

            log_event(
                "SCANNER",
                f"Loaded {len(st.session_state.market_rows)} Solana market rows."
            )

            st.success(
                f"Loaded {len(st.session_state.market_rows)} market rows."
            )

        except Exception as error:

            log_event(
                "SCANNER",
                f"Market scan failed: {error}",
                "ERROR"
            )

            st.error(f"Market scan failed: {error}")

    rows = st.session_state.market_rows

    if rows:

        table = []

        for row in rows:

            passed, reasons = risk_screen(
                row,
                min_liquidity,
                min_volume
            )

            table.append({
                "Token": row["symbol"],
                "Price USD": row["price"],
                "Liquidity": row["liquidity"],
                "Volume 24h": row["volume24"],
                "Buys": row["buys24"],
                "Sells": row["sells24"],
                "1h %": row["change1h"],
                "24h %": row["change24h"],
                "Screen": "PASS" if passed else "FILTER"
            })

        st.dataframe(
            pd.DataFrame(table),
            use_container_width=True,
            hide_index=True
        )

        options = [
            f'{row["symbol"]} · '
            f'{row["address"][:6]}…{row["address"][-4:]}'
            for row in rows
        ]

        chosen = st.selectbox(
            "Select token for paper-trade review",
            options
        )

        row = rows[options.index(chosen)]

        st.session_state.selected_symbol = row["symbol"]

        passed, reasons = risk_screen(
            row,
            min_liquidity,
            min_volume
        )

        st.markdown(
            f'**Selected: {row["symbol"]}** · '
            f'${row["price"]:.10f} · {row["dex"]}'
        )

        if passed:

            st.success(
                "MARKET FILTER PASSED. "
                "This is not a contract safety guarantee."
            )

        else:

            st.warning(
                "FILTERED: " + "; ".join(reasons)
            )

        st.caption(
            f'Liquidity: ${row["liquidity"]:,.0f} · '
            f'Volume: ${row["volume24"]:,.0f} · '
            f'Buys/Sells: {row["buys24"]}/{row["sells24"]}'
        )

        qty_usd = min(
            cash * risk_pct / 100,
            100000
        )

        st.write(
            f"**SIZER:** Maximum simulated order: ${qty_usd:,.2f}"
        )

        buy_col, skip_col = st.columns(2)

        with buy_col:

            if st.button(
                "🟢 PAPER BUY",
                use_container_width=True,
                disabled=(
                    st.session_state.halted
                    or not st.session_state.paper_enabled
                    or not passed
                )
            ):

                if row["price"] <= 0 or qty_usd > cash:

                    st.error("Invalid price or insufficient cash.")

                else:

                    con = db()

                    con.execute(
                        """
                        INSERT INTO trades
                        (ts,symbol,address,side,qty,price,notional,reason)
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            row["symbol"],
                            row["address"],
                            "BUY",
                            qty_usd / row["price"],
                            row["price"],
                            qty_usd,
                            "Market screen passed; paper buy only."
                        )
                    )

                    con.commit()
                    con.close()

                    log_event(
                        "EXECUTOR",
                        f'Paper BUY {row["symbol"]} for ${qty_usd:.2f}.'
                    )

                    st.rerun()

        with skip_col:

            if st.button("SKIP TOKEN", use_container_width=True):

                log_event(
                    "ANALYST",
                    f'Skipped {row["symbol"]} after review.'
                )

                st.info("Token skipped.")

        if row["url"]:

            st.markdown(
                f'[Open pair on DexScreener]({row["url"]})'
            )

    else:

        st.info(
            "Press SCAN to fetch current Solana market data."
        )


# ---------------- RIGHT PANEL ----------------

with right:

    st.markdown("### TRADE · ENTRY / EXIT")

    if rows:

        st.markdown(
            "<div class='terminal-card'>",
            unsafe_allow_html=True
        )

        st.write("**Selected market**")

        st.write(
            st.session_state.selected_symbol
            or "No token selected"
        )

        st.caption(
            "Simulated orders only. No wallet connected."
        )

        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("### EQUITY")

    st.metric(
        "Paper cash balance",
        f"${cash:,.2f}"
    )

    st.caption(
        "Open-position valuation and P&L are not implemented yet."
    )


# ---------------- ACTIVITY LOG ----------------

st.markdown("---")

st.markdown(
    "### ACTIVITY LOG · DECISIONS WITH REASONS"
)

events = get_events()

if events.empty:

    st.caption("No activity yet. Run a scan first.")

else:

    for _, event in events.head(20).iterrows():

        color = (
            "#ff6875"
            if event["level"] in ("ERROR", "STOP")
            else "#22d38a"
        )

        timestamp = str(event["ts"]).replace("T", " ")[:19]

        st.markdown(
            f"""
            <div class="flow">
                <span style="color:{color};font-weight:700">
                    {event["agent"]}
                </span>
                <small>{timestamp} UTC</small><br>
                {event["message"]}
            </div>
            """,
            unsafe_allow_html=True
        )


# ---------------- TRADE HISTORY ----------------

st.markdown("---")

st.markdown("### TRADE HISTORY")

if trades.empty:

    st.caption("No paper orders yet.")

else:

    st.dataframe(
        trades,
        use_container_width=True,
        hide_index=True
    )


st.caption(
    "GrokStockBot V3.0 | DexScreener public market data "
    "| Paper trading only | No real-money execution."
)
