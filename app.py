"""
app.py  –  MaaNiish Arrow  |  Streamlit Dashboard
Run:  streamlit run app.py
"""
import logging
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)

st.set_page_config(
    page_title="MaaNiish Arrow",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

from streamlit_autorefresh import st_autorefresh
st_autorefresh(interval=1_000, limit=None, key="ticker")

from config import DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, ATM_RANGE, DEFAULT_SPOT
from state import app_state

_DEFAULTS = {
    "feed_started":  False,
    "feed_manager":  None,
    # Credentials are intentionally blank in the UI.
    # Set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env for local dev only—
    # never expose them in a public deployment.
    "client_id":     "",
    "access_token":  "",
    "spot_nifty":     DEFAULT_SPOT["NIFTY"],
    "spot_banknifty": DEFAULT_SPOT["BANKNIFTY"],
    "spot_sensex":    DEFAULT_SPOT["SENSEX"],
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

_BEEP_JS = """
<script>
(function(){
  try {
    var ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0,350,700].forEach(function(d){
      var o=ctx.createOscillator(), g=ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type='sine'; o.frequency.value=900;
      g.gain.setValueAtTime(0.4, ctx.currentTime+d/1000);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime+d/1000+0.25);
      o.start(ctx.currentTime+d/1000); o.stop(ctx.currentTime+d/1000+0.25);
    });
  } catch(e){}
})();
</script>
"""

def _build_chart(candles, instrument, all_signals=None):
    """
    Render a Plotly candlestick chart.
    `candles`     – list of candle dicts from app_state (historical + live).
    `all_signals` – list of signal dicts; arrows are drawn for matching signals
                    even when the candle's `signal` flag isn't set (e.g. historical).
    """
    opt   = instrument.get("option_type", "CE")
    sid   = instrument.get("security_id", "")
    title = f"{instrument.get('index','')}  {int(instrument.get('strike_price',0))}  {opt}"
    color = "#4CAF50" if opt == "CE" else "#F44336"
    fig   = go.Figure()

    if not candles:
        fig.add_annotation(
            text="⏳ Loading today's candles… (check back in a few seconds)",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=13, color="#aaa"))
        fig.update_layout(
            title=dict(text=title, font=dict(color=color, size=13)),
            height=340, template="plotly_dark",
            xaxis=dict(visible=False), yaxis=dict(visible=False),
            margin=dict(l=10,r=10,t=36,b=10))
        return fig

    df = pd.DataFrame(candles).sort_values("timestamp").drop_duplicates("timestamp")

    # Candlestick trace
    fig.add_trace(go.Candlestick(
        x=df["timestamp"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="1m Candle",
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        whiskerwidth=0.3))

    # ── MaaNiish Arrow markers ──────────────────────────────────────
    # Mark candles that have signal=True in candle data
    sig_df = df[df.get("signal", pd.Series(False, index=df.index)) == True]

    # Also mark candles matching live signal events (for real-time accuracy)
    if all_signals:
        sig_ts = {
            s["candle_timestamp"]
            for s in all_signals
            if s.get("security_id") == sid
        }
        extra = df[df["timestamp"].isin(sig_ts)]
        sig_df = pd.concat([sig_df, extra]).drop_duplicates("timestamp")

    if not sig_df.empty:
        fig.add_trace(go.Scatter(
            x=sig_df["timestamp"],
            y=sig_df["high"] * 1.005,
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=18, color="gold",
                        line=dict(color="darkorange", width=2)),
            text=["▼"] * len(sig_df),
            textposition="top center",
            textfont=dict(size=11, color="gold"),
            name="▼ MaaNiish",
            hovertemplate=(
                "<b>▼ MaaNiish Arrow</b><br>"
                "Candle: %{x}<br>"
                "High: %{customdata:.2f}<br>"
                "<extra></extra>"
            ),
            customdata=sig_df["high"],
        ))
    fig.update_layout(
        title=dict(text=title, font=dict(color=color, size=13)),
        xaxis_rangeslider_visible=False,
        height=340,
        margin=dict(l=10, r=10, t=36, b=10),
        template="plotly_dark",
        xaxis=dict(tickformat="%H:%M", gridcolor="#2a2a3a", title="Time (IST)"),
        yaxis=dict(gridcolor="#2a2a3a", side="right"),
        legend=dict(orientation="h", y=-0.15, font=dict(size=10)),
        hovermode="x unified",
    )
    return fig

def _price_table(instruments, all_signals):
    rows = []
    for inst in sorted(instruments, key=lambda x:(x.get("option_type",""),x.get("strike_price",0))):
        sid = inst["security_id"]
        cur = app_state.current_candles.get(sid, {})
        def f(v): return f"{v:.2f}" if isinstance(v, (int,float)) else "—"
        has_sig = any(s.get("security_id")==sid for s in all_signals)
        candles = app_state.get_candles(sid)
        rows.append({
            "Strike":   int(inst.get("strike_price",0)),
            "Type":     inst.get("option_type",""),
            "LTP":      f(cur.get("close","")),
            "C.High":   f(cur.get("high","")),
            "Top Bid":  f(cur.get("top_bid_high","")),
            "▼ Arrow":  "🎯" if has_sig else "—",
            "Candles":  len([c for c in candles if c.get("closed")]),
        })
    return pd.DataFrame(rows)

def _start_feed(client_id, access_token, manual_spots):
    import threading
    from data_feed import DhanFeedManager
    from instrument_manager import InstrumentManager
    from dhanhq import dhanhq as DhanHQ, DhanContext
    from history_loader import preload_into_state

    # Fall back to env-var credentials if UI fields left blank
    effective_id    = client_id    or DHAN_CLIENT_ID
    effective_token = access_token or DHAN_ACCESS_TOKEN
    if not effective_id or not effective_token:
        return False, "Enter your Dhan Client ID and Access Token."

    try:
        ctx  = DhanContext(effective_id, effective_token)
        dhan = DhanHQ(ctx)
    except Exception as exc:
        return False, f"DhanHQ init failed: {exc}"

    # Quick token validation via a lightweight REST call
    try:
        probe = dhan.get_fund_limits()
        if isinstance(probe, dict) and probe.get("status") == "failure":
            err_code = probe.get("remarks", {}).get("error_code", "")
            err_msg  = probe.get("remarks", {}).get("error_message", "")
            if err_code in ("DH-901", "DH-902") or "invalid" in err_msg.lower() or "expired" in err_msg.lower():
                return False, (
                    "Access token is INVALID or EXPIRED (DH-901). "
                    "Please generate a new token at https://web.dhan.co → Profile → API Access "
                    "and update your credentials."
                )
    except Exception:
        pass  # If probe fails for other reasons, let the main feed attempt handle it

    from config import INDEX_SECURITY_IDS
    spot_prices = {}
    for index, info in INDEX_SECURITY_IDS.items():
        try:
            resp = dhan.ticker_data({info["exchange_segment"]: [info["security_id"]]})
            if isinstance(resp,dict) and resp.get("status")=="success":
                recs = resp.get("data",{}).get(info["exchange_segment"],[])
                if recs:
                    ltp = float(recs[0].get("last_price",0) or 0)
                    if ltp>0: spot_prices[index]=ltp
        except: pass
    for idx, fb in manual_spots.items():
        if idx not in spot_prices or spot_prices[idx]<=0:
            spot_prices[idx] = fb
    for idx, price in spot_prices.items():
        app_state.update_spot_price(idx, price)

    mgr = InstrumentManager()
    try: mgr.load_master()
    except Exception as exc: return False, f"Master download failed: {exc}"
    instruments = mgr.build_subscriptions(spot_prices, ATM_RANGE)
    if not instruments:
        return False, "No instruments found. Adjust spot prices and retry."

    # Start live WebSocket feed
    feed = DhanFeedManager(effective_id, effective_token)
    feed.start(instruments)
    st.session_state.feed_manager = feed

    # Pre-load today's historical 1-min candles in a background thread
    # so charts are populated immediately without waiting for live ticks
    threading.Thread(
        target=preload_into_state,
        args=(dhan, instruments, app_state),
        daemon=True,
        name="HistoryLoader",
    ).start()

    return True, f"Subscribed to {len(instruments)} instruments across NIFTY / BANKNIFTY / SENSEX"

# Poll alerts
_new_alerts = []
if st.session_state.feed_manager:
    _new_alerts = st.session_state.feed_manager.pop_pending_alerts()
if _new_alerts:
    st.components.v1.html(_BEEP_JS, height=0)
    for sig in _new_alerts:
        st.toast(f"▼ {sig['index']} {int(sig['strike'])} {sig['option_type']}  High {sig['candle_high']:.2f} → Bid {sig['top_bid']:.2f} (+{sig['excess']:.2f})", icon="🎯")

# ── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎯 MaaNiish Arrow")
    st.caption("Live Market-Depth Signal Tracker")
    st.divider()
    st.markdown("### API Credentials")
    st.caption("🔒 Credentials are never stored or logged. Clear your browser session when done.")
    cid   = st.text_input("Client ID",
                          value=st.session_state.client_id,
                          placeholder="Enter Dhan Client ID",
                          key="in_cid")
    token = st.text_input("Access Token",
                          value=st.session_state.access_token,
                          placeholder="Enter Dhan Access Token",
                          type="password",
                          key="in_tok")
    # Save to session state only (never persisted to disk or logs)
    if cid   != st.session_state.client_id:    st.session_state.client_id    = cid
    if token != st.session_state.access_token: st.session_state.access_token = token
    st.divider()
    with st.expander("Manual Spot Prices (fallback)"):
        st.caption("Used if REST API can't fetch live spot.")
        st.session_state.spot_nifty     = st.number_input("NIFTY",     value=st.session_state.spot_nifty,     step=50)
        st.session_state.spot_banknifty = st.number_input("BANKNIFTY", value=st.session_state.spot_banknifty, step=100)
        st.session_state.spot_sensex    = st.number_input("SENSEX",    value=st.session_state.spot_sensex,    step=100)
    st.divider()
    if app_state.connected:
        st.success("● Feed connected")
    elif st.session_state.feed_started:
        err = app_state.connection_error or "Connecting…"
        if "429" in err or "Retrying" in err:
            st.warning(f"⏳ {err}")
        elif "EXPIRED" in err or "INVALID" in err or "DH-901" in err:
            st.error("🔑 Token expired! Generate a new one at web.dhan.co → Profile → API Access")
        else:
            st.error(f"✗ {err}")
    else:
        st.info("○ Not started")
    if app_state.last_update:
        st.caption(f"Last tick: {app_state.last_update.strftime('%H:%M:%S')}")
    st.divider()
    # Guard: only show Start button when no feed is running at all
    feed_active = st.session_state.feed_started or app_state.connected
    if not feed_active:
        if st.button("▶ Start Feed", type="primary", use_container_width=True):
            if not cid or not token:
                st.error("Enter credentials first.")
            else:
                st.session_state.client_id    = cid
                st.session_state.access_token = token
                manual = {"NIFTY":st.session_state.spot_nifty,"BANKNIFTY":st.session_state.spot_banknifty,"SENSEX":st.session_state.spot_sensex}
                with st.spinner("Connecting…"):
                    ok, msg = _start_feed(cid, token, manual)
                if ok:
                    st.session_state.feed_started = True
                    st.success(msg)
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error(msg)
    else:
        if app_state.connected:
            st.button("■ Feed Running", disabled=True, use_container_width=True)
        else:
            st.button("⏳ Reconnecting…", disabled=True, use_container_width=True)
        c1,c2 = st.columns(2)
        c1.metric("Instruments", len(app_state.get_instruments()))
        c2.metric("Signals",     len(app_state.get_all_signals()))
    st.divider()
    show_ce = st.checkbox("Show CE", value=True)
    show_pe = st.checkbox("Show PE", value=True)

# ── WELCOME ──────────────────────────────────────────────────────────────────
if not st.session_state.feed_started:
    st.title("🎯 MaaNiish Arrow")
    st.markdown("""
    ### Setup
    1. Enter **Client ID** and **Access Token** in the sidebar.
    2. *(Optional)* Set manual spot prices as fallback.
    3. Click **▶ Start Feed**.

    ---
    ### Signal Logic
    > **If ANY Top Bid during a 1-min candle > that candle's High**
    > → **▼ Arrow** appears above that candle + **sound + popup** fires immediately
    > → **Non-repainting** (fires once per candle, never removed)
    """)
    st.stop()

# ── MAIN CONTENT ─────────────────────────────────────────────────────────────
all_signals = app_state.get_all_signals()

# Header metrics
h1,h2,h3,h4 = st.columns([3,1,1,1])
h1.markdown("## 🎯 MaaNiish Arrow — Live")
h2.metric("Instruments", len(app_state.get_instruments()))
h3.metric("Signals Today", len(all_signals))
h4.metric("Last Tick", app_state.last_update.strftime("%H:%M:%S") if app_state.last_update else "—")
st.divider()

# ── TABS ─────────────────────────────────────────────────────────────────────
t_nifty, t_bank, t_sensex, t_log = st.tabs(["📈 NIFTY", "🏦 BANKNIFTY", "💹 SENSEX", "📋 All Signals"])

for tab, idx_name in zip([t_nifty, t_bank, t_sensex], ["NIFTY","BANKNIFTY","SENSEX"]):
    with tab:
        instruments = app_state.get_instruments_for_index(idx_name)
        if not instruments:
            st.info(f"⏳ Loading {idx_name} instruments…")
            continue

        expiry_str = str(instruments[0].get("expiry_date","—"))
        spot_val   = app_state.spot_prices.get(idx_name, 0)
        idx_sigs   = [s for s in all_signals if s.get("index")==idx_name]

        m1,m2,m3,m4 = st.columns(4)
        m1.metric("Expiry",   expiry_str)
        m2.metric("Spot",     f"{spot_val:,.0f}" if spot_val else "—")
        m3.metric("Tracking", f"{len(instruments)} options")
        m4.metric("Signals",  len(idx_sigs))

        # Live price table
        with st.expander("📊 Live Prices — All Strikes (ATM ±5)", expanded=True):
            st.dataframe(
                _price_table(instruments, all_signals),
                use_container_width=True,
                hide_index=True,
                height=min(40+len(instruments)*35, 450),
            )

        # Charts
        st.markdown("---")
        st.markdown("#### 📉 Candlestick Charts (select strike below)")
        ces = sorted([i for i in instruments if i["option_type"]=="CE"], key=lambda x:x["strike_price"])
        pes = sorted([i for i in instruments if i["option_type"]=="PE"], key=lambda x:x["strike_price"])
        col_ce, col_pe = st.columns(2, gap="medium")

        for col, opt_type, inst_list, show in [(col_ce,"CE",ces,show_ce),(col_pe,"PE",pes,show_pe)]:
            if not inst_list or not show: continue
            with col:
                st.markdown(f"**{opt_type}**")
                strikes  = [int(i["strike_price"]) for i in inst_list]
                selected = st.selectbox("Strike", strikes, index=len(strikes)//2,
                                        key=f"sel_{idx_name}_{opt_type}", label_visibility="collapsed")
                inst = next((i for i in inst_list if int(i["strike_price"])==selected), None)
                if not inst: continue
                candles = app_state.get_candles(inst["security_id"])
                st.plotly_chart(_build_chart(candles, inst, all_signals=all_signals),
                                use_container_width=True, key=f"chart_{inst['security_id']}")
                cur = app_state.current_candles.get(inst["security_id"], {})
                if cur:
                    ltp_v = cur.get("close",0); high_v = cur.get("high",0); bid_v = cur.get("top_bid_high",0)
                    delta = round(bid_v-high_v,2) if bid_v and high_v else None
                    ca,cb,cc = st.columns(3)
                    ca.metric("LTP",     f"{ltp_v:.2f}"  if ltp_v  else "—")
                    cb.metric("C. High", f"{high_v:.2f}" if high_v else "—")
                    cc.metric("Top Bid", f"{bid_v:.2f}"  if bid_v  else "—",
                              delta=f"+{delta}" if delta and delta>0 else None)
                inst_sigs = [s for s in all_signals if s.get("security_id")==inst["security_id"]]
                if inst_sigs:
                    last = inst_sigs[-1]
                    st.success(f"▼ {last['candle_timestamp'].strftime('%H:%M')}  High {last['candle_high']:.2f} → Bid {last['top_bid']:.2f} (+{last['excess']:.2f})")

# All signals tab
with t_log:
    st.markdown("### 📋 All Signals Log")
    if not all_signals:
        st.info("No signals yet — monitoring. Alert fires when **Top Bid > Candle High**.")
    else:
        sdf = pd.DataFrame(all_signals).sort_values("fired_at", ascending=False)
        sdf["Time"]       = sdf["candle_timestamp"].apply(lambda x: x.strftime("%H:%M") if hasattr(x,"strftime") else str(x))
        sdf["Instrument"] = sdf.apply(lambda r: f"{r['index']} {int(r['strike'])} {r['option_type']}", axis=1)
        sdf["Candle High"]= sdf["candle_high"].round(2)
        sdf["Top Bid"]    = sdf["top_bid"].round(2)
        sdf["Bid>High"]   = sdf["excess"].round(2)
        st.dataframe(sdf[["Time","Instrument","Candle High","Top Bid","Bid>High"]],
                     use_container_width=True, hide_index=True, height=500)
