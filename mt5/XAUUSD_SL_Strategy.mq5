//+------------------------------------------------------------------+
//| XAUUSD_SL_Strategy.mq5                                           |
//| Python ブリッジ連携 + ルールベース買い専用 EA                      |
//|                                                                  |
//| 設置手順:                                                         |
//|  1. MQL5/Experts/ にコピー                                        |
//|  2. F7 でコンパイル                                               |
//|  3. チャートにアタッチ                                             |
//|  4. Python: python mt5_ea_bridge.py を起動                       |
//|                                                                  |
//| ルール適用（trading_rules.json）:                                 |
//|  - 買いのみ（売りは構造的損失のため禁止）                          |
//|  - score < InpMinScore のシグナルはスキップ                       |
//|  - 連続損失 >= InpMaxConsecLoss で当日取引停止                    |
//+------------------------------------------------------------------+
#property copyright "XAUUSD SL Strategy"
#property version   "2.00"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//--- 入力パラメータ
input string InpSignalFile    = "signal.json";
input string InpStateFile     = "ea_state.json";
input double InpLotSize       = 0.05;    // デフォルトロット（signal.jsonで上書き）
input int    InpTimerSec      = 5;
input int    InpMagic         = 20240101;
input int    InpMinScore      = 30;      // エントリー最低スコア
input int    InpMaxConsecLoss = 3;       // 連続損失上限（超えたら当日停止）
input bool   InpDebugLog      = true;

CTrade        g_trade;
CPositionInfo g_pos;
datetime      g_last_ts       = 0;
double        g_lot_size      = 0.0;

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   EventSetTimer(InpTimerSec);
   if(InpDebugLog) Print("[EA] 起動  Signal=", InpSignalFile,
                          "  MinScore=", InpMinScore,
                          "  MaxConsecLoss=", InpMaxConsecLoss);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(InpDebugLog) Print("[EA] 停止 reason=", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   string action, sig_ts, strength;
   double sl_price, tp_price, atr_v, sl_multi, rsi_exit, trail_m, lot_sig;
   int    max_slip, score, tp_hold_min;

   if(!ReadSignal(action, sl_price, tp_price, atr_v, sl_multi,
                  max_slip, rsi_exit, trail_m, lot_sig, score, strength,
                  tp_hold_min, sig_ts))
   { if(InpDebugLog) Print("[EA] signal.json 読み込み失敗"); return; }

   datetime sig_dt = StringToTime(sig_ts);
   if(sig_dt <= g_last_ts) return;   // 同タイムスタンプはスキップ
   g_last_ts = sig_dt;

   g_lot_size = (lot_sig > 0.0) ? lot_sig : InpLotSize;
   if(max_slip > 0) g_trade.SetDeviationInPoints(max_slip);

   // トレーリング SL 更新（ポジション保有中）
   UpdateTrailing(atr_v, trail_m);

   // 連続損失チェック
   int consec = GetConsecLosses();
   if(consec >= InpMaxConsecLoss)
   {
      if(InpDebugLog)
         Print("[EA] 連続損失=", consec, "回 >= ", InpMaxConsecLoss, " → 当日取引停止");
      WriteState(consec);
      return;
   }

   // スコアチェック
   if(score < InpMinScore)
   {
      if(InpDebugLog)
         Print("[EA] スコア=", score, " < ", InpMinScore, " → スキップ");
      WriteState(consec);
      return;
   }

   // 新規エントリー（ポジションなし時のみ、買いのみ）
   if(CountPos() == 0 && action == "buy")
      OpenBuy(sl_price, tp_price);

   WriteState(consec);
}

//+------------------------------------------------------------------+
void OpenBuy(double sl, double tp)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(sl >= ask) { Print("[EA] Buy スキップ: SL(", sl, ") >= Ask(", ask, ")"); return; }
   if(!g_trade.Buy(g_lot_size, _Symbol, ask, sl, tp, "SL_BUY"))
      Print("[EA] Buy 失敗: ", g_trade.ResultRetcode());
   else
      Print("[EA] Buy 執行  lot=", g_lot_size, " ask=", ask, " SL=", sl, " TP=", tp);
}

//+------------------------------------------------------------------+
void UpdateTrailing(double atr, double trail_multi)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic()  != InpMagic)  continue;
      if(g_pos.Symbol() != _Symbol)   continue;
      if(g_pos.PositionType() != POSITION_TYPE_BUY) continue;

      double cur_sl = g_pos.StopLoss();
      double cur_tp = g_pos.TakeProfit();
      double trail  = atr * trail_multi;
      double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double new_sl = NormalizeDouble(bid - trail, _Digits);
      if(new_sl > cur_sl + _Point)
      {
         g_trade.PositionModify(g_pos.Ticket(), new_sl, cur_tp);
         if(InpDebugLog) Print("[EA] Trail BUY ", cur_sl, "→", new_sl);
      }
   }
}

int CountPos()
{
   int cnt = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
      if(g_pos.SelectByIndex(i) && g_pos.Magic()==InpMagic && g_pos.Symbol()==_Symbol)
         cnt++;
   return cnt;
}

// 直近クローズ済みトレードから連続損失回数を取得
int GetConsecLosses()
{
   int consec = 0;
   if(!HistorySelect(0, TimeCurrent())) return 0;
   int total = HistoryDealsTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC)  != InpMagic) continue;
      if(HistoryDealGetString(ticket,  DEAL_SYMBOL) != _Symbol)  continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY)  != DEAL_ENTRY_OUT) continue;
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT)
                    + HistoryDealGetDouble(ticket, DEAL_SWAP)
                    + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      if(profit < 0.0) consec++;
      else             break;
   }
   return consec;
}

//+------------------------------------------------------------------+
// signal.json 読み込み（簡易 JSON パーサ）
//+------------------------------------------------------------------+
bool ReadSignal(string &action, double &sl, double &tp,
                double &atr, double &sl_multi,
                int &max_slip, double &rsi_exit, double &trail_m,
                double &lot_size, int &score, string &strength,
                int &tp_hold_min, string &ts)
{
   int fh = FileOpen(InpSignalFile, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) return false;
   string raw = "";
   while(!FileIsEnding(fh)) raw += FileReadString(fh);
   FileClose(fh);
   if(StringLen(raw) < 10) return false;

   action      = JStr(raw, "action");
   sl          = JDbl(raw, "sl_price");
   tp          = JDbl(raw, "tp_price");
   atr         = JDbl(raw, "atr");
   sl_multi    = JDbl(raw, "sl_multi");
   max_slip    = (int)JDbl(raw, "max_slip_pt");
   rsi_exit    = JDbl(raw, "rsi_exit_thr");
   trail_m     = JDbl(raw, "trail_multi");
   lot_size    = JDbl(raw, "lot_size");
   score       = (int)JDbl(raw, "score");
   strength    = JStr(raw, "strength");
   tp_hold_min = (int)JDbl(raw, "tp_hold_minutes");
   ts          = JStr(raw, "timestamp");
   return StringLen(action) > 0;
}

string JStr(const string &j, const string &k)
{
   string pat = "\"" + k + "\":";
   int p = StringFind(j, pat);
   if(p < 0) return "";
   int s = p + StringLen(pat);
   while(s < StringLen(j) && StringSubstr(j,s,1)==" ") s++;
   if(StringSubstr(j,s,1) != "\"") return "";
   s++;
   int e = StringFind(j, "\"", s);
   if(e < 0) return "";
   return StringSubstr(j, s, e-s);
}

double JDbl(const string &j, const string &k)
{
   string pat = "\"" + k + "\":";
   int p = StringFind(j, pat);
   if(p < 0) return 0.0;
   int s = p + StringLen(pat);
   while(s < StringLen(j) && StringSubstr(j,s,1)==" ") s++;
   int e = s;
   while(e < StringLen(j))
   {
      string c = StringSubstr(j, e, 1);
      if(c==","||c=="}"||c=="\n"||c==" "||c=="\r") break;
      e++;
   }
   return StringToDouble(StringSubstr(j, s, e-s));
}

//+------------------------------------------------------------------+
// ea_state.json 書き込み（Python が読む状態ファイル）
//+------------------------------------------------------------------+
void WriteState(int consec_losses)
{
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   int    pos = CountPos();
   string json = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"positions\":%d,"
      "\"consecutive_losses\":%d,"
      "\"timestamp\":\"%s\",\"symbol\":\"%s\",\"magic\":%d}",
      bal, eq, pos, consec_losses,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
      _Symbol, InpMagic);

   int fh = FileOpen(InpStateFile, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, json);
   FileClose(fh);
}
//+------------------------------------------------------------------+
