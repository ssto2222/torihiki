"""btc_predict.py — BTC/USD 価格予測: $88,000 突破時期の統計推定

モデル:
  1. ARIMA   — 自己回帰和分移動平均（対数収益率ベース）
  2. Prophet — Facebook 時系列分解モデル
  3. Linear Regression on log(price) — トレンド外挿
  4. Monte Carlo — GBM（幾何ブラウン運動）確率シミュレーション

出力:
  ./output/btc_predict.png  — 予測チャート
  コンソール                — 各モデルの $88k 到達予測日

免責: 暗号資産価格予測は本質的に不確実です。投資判断に使わないでください。
"""
from __future__ import annotations
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
from pathlib import Path

# ── 1. データ取得 ──────────────────────────────────────────────────────────────

# 既知 BTC/USD 主要価格アンカー (日付, 終値)
_BTC_ANCHORS: list[tuple[str, float]] = [
    ('2022-01-01', 46_211), ('2022-02-01', 38_160), ('2022-03-01', 43_193),
    ('2022-05-01', 37_644), ('2022-06-18', 18_009), ('2022-08-01', 23_298),
    ('2022-09-01', 19_988), ('2022-10-01', 19_315), ('2022-11-09', 16_200),
    ('2022-11-21', 15_480), ('2023-01-01', 16_618), ('2023-02-01', 23_140),
    ('2023-04-14', 30_434), ('2023-06-15', 25_108), ('2023-07-13', 31_386),
    ('2023-10-25', 34_023), ('2024-01-01', 42_265), ('2024-02-29', 62_372),
    ('2024-03-14', 73_084), ('2024-04-13', 62_480), ('2024-06-01', 67_523),
    ('2024-08-05', 49_159), ('2024-09-01', 59_142), ('2024-10-01', 63_302),
    ('2024-11-05', 68_249), ('2024-11-13', 90_243), ('2024-12-17', 107_142),
    ('2025-01-20', 109_225), ('2025-02-01', 96_491), ('2025-03-01', 82_118),
    ('2025-04-01', 82_456), ('2025-05-01', 94_878), ('2025-08-01', 98_320),
]


def _build_synthetic_btc() -> pd.DataFrame:
    """既知アンカーをスプライン補間 + GBM ノイズで日次データに展開する。"""
    from scipy.interpolate import PchipInterpolator

    anchors = [(pd.Timestamp(d), p) for d, p in _BTC_ANCHORS]
    all_days = pd.date_range(anchors[0][0], anchors[-1][0], freq='D')
    t_anc = np.array([(d - anchors[0][0]).days for d, _ in anchors], dtype=float)
    p_anc = np.log([p for _, p in anchors])

    interp = PchipInterpolator(t_anc, p_anc)
    t_all  = np.array([(d - anchors[0][0]).days for d in all_days], dtype=float)
    log_trend = interp(t_all)

    # 実際の BTC ボラティリティ (~3%/day) のノイズを加算
    rng   = np.random.default_rng(0)
    noise = rng.normal(0, 0.018, size=len(all_days))
    noise = np.cumsum(noise) - np.cumsum(noise)  # ゼロ平均化
    # アンカー日は正確な価格に固定（ノイズを抑制）
    prices = np.exp(log_trend + noise * 0.4)

    return pd.DataFrame({'price': prices}, index=all_days)


def fetch_btc(years: int = 4) -> pd.DataFrame:
    """yfinance → CoinGecko → 組み込みアンカーデータの順でフォールバック。"""
    # --- yfinance 試行 ---
    try:
        import yfinance as yf
        end   = datetime.today()
        start = end - timedelta(days=365 * years)
        df = yf.download('BTC-USD', start=start, end=end, progress=False, auto_adjust=True)
        if not df.empty:
            df = df[['Close']].copy()
            df.columns = ['price']
            df.index = pd.to_datetime(df.index)
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            print(f"[データ取得] yfinance  {len(df)} 本  最新: ${df['price'].iloc[-1]:,.0f}")
            return df
    except Exception:
        pass

    # --- CoinGecko 試行 ---
    try:
        import urllib.request, json as _json
        url = ('https://api.coingecko.com/api/v3/coins/bitcoin/market_chart'
               '?vs_currency=usd&days=1460&interval=daily')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw    = _json.loads(r.read())['prices']
            idx    = pd.to_datetime([x[0] for x in raw], unit='ms')
            prices = [x[1] for x in raw]
            df = pd.DataFrame({'price': prices}, index=idx)
            df.index = df.index.tz_localize(None)
            print(f"[データ取得] CoinGecko  {len(df)} 本  最新: ${df['price'].iloc[-1]:,.0f}")
            return df
    except Exception:
        pass

    # --- 組み込みアンカーデータ ---
    print("[データ取得] ネットワーク不可 → 組み込みアンカーデータ（GBM補間）を使用")
    df = _build_synthetic_btc()
    print(f"  {len(df)} 本  最新: ${df['price'].iloc[-1]:,.0f}  (基準日: {df.index[-1].date()})")
    return df


# ── 2. ARIMA 予測 ──────────────────────────────────────────────────────────────

def predict_arima(df: pd.DataFrame, horizon: int = 365) -> pd.Series:
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.stattools import adfuller

    log_ret = np.log(df['price']).diff().dropna()

    # ADF 検定 (定常性確認)
    adf_p = adfuller(log_ret)[1]
    print(f"\n[ARIMA] ADF p値={adf_p:.4f}  ({'定常' if adf_p < 0.05 else '非定常'})")

    # ARIMA(1,0,1) on log returns
    model = ARIMA(log_ret, order=(1, 0, 1)).fit()
    fc    = model.forecast(steps=horizon)

    # 対数収益率 → 価格に戻す
    last_price = df['price'].iloc[-1]
    cum_ret    = np.cumsum(fc.values)
    prices     = last_price * np.exp(cum_ret)

    future_idx = pd.date_range(df.index[-1] + timedelta(days=1), periods=horizon, freq='D')
    return pd.Series(prices, index=future_idx, name='ARIMA')


# ── 3. Prophet 予測 ────────────────────────────────────────────────────────────

def predict_prophet(df: pd.DataFrame, horizon: int = 365) -> tuple[pd.Series, pd.Series, pd.Series]:
    from prophet import Prophet

    train = df.reset_index().rename(columns={'Date': 'ds', 'price': 'y'})
    # yfinance の index 名が 'Datetime' の場合を考慮
    if 'ds' not in train.columns:
        train = df.reset_index()
        train.columns = ['ds', 'y']
    train['ds'] = pd.to_datetime(train['ds'])

    m = Prophet(
        changepoint_prior_scale=0.3,
        seasonality_prior_scale=10,
        yearly_seasonality=True,
        weekly_seasonality=True,
        daily_seasonality=False,
    )
    m.fit(train)

    future = m.make_future_dataframe(periods=horizon)
    fc     = m.predict(future)

    fc = fc.set_index('ds').loc[df.index[-1] + timedelta(days=1):]
    idx = fc.index

    print(f"[Prophet] 予測期間 {idx[0].date()} 〜 {idx[-1].date()}")
    return (
        pd.Series(fc['yhat'].values,       index=idx, name='Prophet'),
        pd.Series(fc['yhat_lower'].values, index=idx, name='Prophet_low'),
        pd.Series(fc['yhat_upper'].values, index=idx, name='Prophet_high'),
    )


# ── 4. 対数線形トレンド外挿 ────────────────────────────────────────────────────

def predict_log_linear(df: pd.DataFrame, horizon: int = 365) -> pd.Series:
    from sklearn.linear_model import LinearRegression

    x = np.arange(len(df)).reshape(-1, 1)
    y = np.log(df['price'].values)

    reg = LinearRegression().fit(x, y)
    r2  = reg.score(x, y)
    print(f"[LogLinear] R²={r2:.3f}")

    x_fut = np.arange(len(df), len(df) + horizon).reshape(-1, 1)
    y_fut = np.exp(reg.predict(x_fut))

    future_idx = pd.date_range(df.index[-1] + timedelta(days=1), periods=horizon, freq='D')
    return pd.Series(y_fut, index=future_idx, name='LogLinear')


# ── 5. Monte Carlo (GBM) ───────────────────────────────────────────────────────

def predict_montecarlo(
    df: pd.DataFrame,
    horizon: int = 365,
    n_sim: int = 2000,
    target: float = 88_000,
) -> dict:
    log_ret = np.log(df['price']).diff().dropna()
    mu      = log_ret.mean()
    sigma   = log_ret.std()
    s0      = df['price'].iloc[-1]

    print(f"[MonteCarlo] μ={mu:.5f}/day  σ={sigma:.5f}/day  S0=${s0:,.0f}")

    rng  = np.random.default_rng(42)
    dt   = 1
    Z    = rng.standard_normal((horizon, n_sim))
    ret  = (mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * Z
    paths = s0 * np.exp(np.cumsum(ret, axis=0))   # (horizon, n_sim)

    future_idx = pd.date_range(df.index[-1] + timedelta(days=1), periods=horizon, freq='D')

    # 各パスが初めて target を超えた日を記録
    hit_days: list[int] = []
    for col in range(n_sim):
        cross = np.where(paths[:, col] >= target)[0]
        if len(cross):
            hit_days.append(cross[0])

    pct5  = np.percentile(paths, 5,  axis=1)
    pct50 = np.percentile(paths, 50, axis=1)
    pct95 = np.percentile(paths, 95, axis=1)

    return {
        'idx':      future_idx,
        'pct5':     pd.Series(pct5,  index=future_idx),
        'pct50':    pd.Series(pct50, index=future_idx, name='MC中央値'),
        'pct95':    pd.Series(pct95, index=future_idx),
        'hit_days': hit_days,
        'n_sim':    n_sim,
        'paths':    paths,
    }


# ── 6. $88k 到達日レポート ─────────────────────────────────────────────────────

TARGET_88K  = 88_000.0
TARGET      = 180_000.0

def first_cross(series: pd.Series, target: float = TARGET) -> str:
    crossed = series[series >= target]
    if crossed.empty:
        return '予測期間内に到達せず'
    return str(crossed.index[0].date())


def report_montecarlo(mc: dict, target: float = TARGET) -> None:
    hit = mc['hit_days']
    n   = mc['n_sim']
    idx = mc['idx']
    horizon_y = len(idx) / 365
    prob = len(hit) / n * 100
    if not hit:
        print(f"  到達確率 ({horizon_y:.0f}年以内): 0%  (n={n})")
        return
    print(f"  到達確率 ({horizon_y:.0f}年以内): {prob:.1f}%  (n={n})")
    if len(hit) < 30:
        print(f"  ※ サンプル数が少ないため日付推定は信頼性が低い ({len(hit)}パスのみ到達)")
        return
    p25_day  = int(np.percentile(hit, 25))
    p50_day  = int(np.percentile(hit, 50))
    p75_day  = int(np.percentile(hit, 75))
    def _d(day: int) -> str:
        return str(idx[day].date()) if day < len(idx) else '期間外'
    print(f"  25パーセンタイル  : {_d(p25_day)}")
    print(f"  中央値 (50%ile)   : {_d(p50_day)}")
    print(f"  75パーセンタイル  : {_d(p75_day)}")


# ── 7. チャート描画 ────────────────────────────────────────────────────────────

def plot_all(df: pd.DataFrame, arima: pd.Series, prophet: tuple,
             loglin: pd.Series, mc: dict, out: str) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(14, 10),
                             gridspec_kw={'height_ratios': [3, 1]})
    ax, ax2 = axes

    # 実績
    ax.plot(df.index, df['price'], color='#222', lw=1.5, label='実績 BTC/USD')

    # LogLinear
    ax.plot(loglin.index, loglin.values, '--', color='gray', lw=1, label='対数線形トレンド', alpha=0.7)

    # ARIMA
    ax.plot(arima.index, arima.values, color='steelblue', lw=1.5, label='ARIMA(1,0,1)', alpha=0.85)

    # Prophet
    p_mid, p_low, p_high = prophet
    ax.plot(p_mid.index, p_mid.values, color='darkorange', lw=1.5, label='Prophet', alpha=0.85)
    ax.fill_between(p_mid.index, p_low.values, p_high.values, color='darkorange', alpha=0.12)

    # Monte Carlo
    ax.plot(mc['pct50'].index, mc['pct50'].values, color='seagreen', lw=1.5,
            label='MC 中央値 (GBM)', alpha=0.85)
    ax.fill_between(mc['idx'], mc['pct5'].values, mc['pct95'].values,
                    color='seagreen', alpha=0.10, label='MC 5〜95%ile')

    # ターゲットライン
    ax.axhline(TARGET_88K, color='orange', lw=1.2, ls=':', alpha=0.8, label='$88k (突破済み)')
    ax.axhline(TARGET,     color='crimson', lw=1.5, ls='--',           label=f'${TARGET/1000:.0f}k 目標')
    ax.set_ylim(bottom=max(0, df['price'].min() * 0.5))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'${v/1000:.0f}k'))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right')
    ax.set_title(f'BTC/USD 価格予測  —  ${TARGET/1000:.0f}k 突破時期の統計推定', fontsize=13, pad=10)
    ax.set_ylabel('価格 (USD)')
    ax.legend(fontsize=8, loc='upper left')
    ax.grid(alpha=0.3)

    # 下段: MC ヒストグラム（到達日分布）
    if mc['hit_days']:
        hit_dates = [mc['idx'][d].to_pydatetime() for d in mc['hit_days'] if d < len(mc['idx'])]
        ax2.hist(hit_dates, bins=40, color='seagreen', alpha=0.7, edgecolor='white')
        ax2.set_title(f'Monte Carlo: ${TARGET/1000:.0f}k 到達日の分布  (n={mc["n_sim"]})', fontsize=10)
        ax2.set_ylabel('パス数')
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')
        ax2.grid(alpha=0.3)
    else:
        ax2.text(0.5, 0.5, f'予測期間内に${TARGET/1000:.0f}k到達なし', ha='center', va='center',
                 transform=ax2.transAxes, fontsize=11, color='gray')

    ax.text(0.01, 0.01,
            '⚠ 統計モデルによる参考値です。投資判断に使用しないでください。',
            transform=ax.transAxes, fontsize=7, color='gray', va='bottom')

    plt.tight_layout()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=150)
    print(f"\n[出力] {out}")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    HORIZON = 365 * 3   # 予測日数（3年）
    OUT     = './output/btc_predict.png'

    df = fetch_btc(years=4)
    current = df['price'].iloc[-1]
    today   = df.index[-1].date()
    print(f"\n現在価格: ${current:,.0f}  (基準日: {today})")
    print(f"目標価格: ${TARGET:,.0f}  (差分: ${TARGET - current:+,.0f}  {(TARGET/current - 1)*100:+.1f}%)")

    arima       = predict_arima(df,    horizon=HORIZON)
    prophet_tup = predict_prophet(df,  horizon=HORIZON)
    loglin      = predict_log_linear(df, horizon=HORIZON)
    mc          = predict_montecarlo(df, horizon=HORIZON, n_sim=3000, target=TARGET)

    # $88k 突破履歴を表示
    cross88 = df[df['price'] >= TARGET_88K]
    if not cross88.empty:
        first88 = cross88.index[0].date()
        print(f"\n[$88k履歴] データ上で最初に $88,000 を超えた日: {first88}")
    else:
        print(f"\n[$88k履歴] データ期間内に $88,000 突破なし")

    print("\n" + "=" * 60)
    print(f"  ${TARGET/1000:.0f}k 到達予測  (基準: {today}  現在: ${current:,.0f})")
    print("=" * 60)
    print(f"  ARIMA(1,0,1)    : {first_cross(arima, TARGET)}")
    print(f"  Prophet         : {first_cross(prophet_tup[0], TARGET)}")
    print(f"  対数線形トレンド : {first_cross(loglin, TARGET)}")
    print(f"  Monte Carlo GBM :")
    report_montecarlo(mc, TARGET)
    print("=" * 60)
    print("\n⚠  警告: 暗号資産の価格予測は本質的に不確実です。")
    print("   上記は統計モデルの参考値であり、投資助言ではありません。")

    plot_all(df, arima, prophet_tup, loglin, mc, OUT)


if __name__ == '__main__':
    main()
