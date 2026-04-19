//+------------------------------------------------------------------+
//| XAUUSD_SL_Strategy.mq5                                           |
//| Python ブリッジ連携 + ボラ適応型 SL EA                            |
//|                                                                  |
//| 設置手順:                                                         |
//|  1. MQL5/Experts/ にコピー                                        |
//|  2. F7 でコンパイル                                               |
//|  3. チャートにアタッチ                                             |
//|  4. Python: python mt5_ea_bridge.py を起動                       |
//+------------------------------------------------------------------+
#property copyright "XAUUSD SL Strategy"
#property version   "1.10"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//--- 入力パラメータ
input string InpSignalFile  = "signal.json";
input string InpStateFile   = "ea_state.json";
input double InpLotSize     = 0.1;
input int    InpTimerSec    = 5;
input bool   InpAllowBuy    = true;
input bool   InpAllowSell   = true;
input int    InpMagic       = 20240101;
input bool   InpDebugLog    = true;

CTrade        g_trade;
CPositionInfo g_pos;
datetime      g_last_ts     = 0;
string        g_last_action = "none";

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   EventSetTimer(InpTimerSec);
   if(InpDebugLog) Print("[EA] 起動  Signal=", InpSignalFile);
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
   string action, sig_ts;
   double sl_price, tp_price, atr_v, atr_ratio, sl_multi, rsi_exit, trail_m;
   int    max_slip;

   if(!ReadSignal(action, sl_price, tp_price, atr_v, atr_ratio,
                  sl_multi, max_slip, rsi_exit, trail_m, sig_ts))
   { if(InpDebugLog) Print("[EA] signal.json 読み込み失敗"); return; }

   datetime sig_dt = StringToTime(sig_ts);
   if(sig_dt <= g_last_ts) return;   // 同タイムスタンプはスキップ
   g_last_ts = sig_dt;

   g_trade.SetDeviationInPoints(max_slip);

   // トレーリング SL 更新（毎ティック）
   UpdateTrailing(atr_v, trail_m);

   // 新規エントリー（ポジションなし時のみ）
   if(CountPos() == 0)
   {
      if(action == "buy"  && InpAllowBuy)  OpenBuy(sl_price, tp_price);
      if(action == "sell" && InpAllowSell) OpenSell(sl_price, tp_price);
   }

   WriteState();
}

//+------------------------------------------------------------------+
void OpenBuy(double sl, double tp)
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(sl >= ask) { Print("[EA] Buy スキップ: SL(", sl, ") >= Ask(", ask, ")"); return; }
   if(!g_trade.Buy(InpLotSize, _Symbol, ask, sl, tp, "XAUUSD_BUY"))
      Print("[EA] Buy 失敗: ", g_trade.ResultRetcode());
   else
      Print("[EA] Buy 執行  ask=", ask, " SL=", sl, " TP=", tp);
}

void OpenSell(double sl, double tp)
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(sl <= bid) { Print("[EA] Sell スキップ: SL(", sl, ") <= Bid(", bid, ")"); return; }
   if(!g_trade.Sell(InpLotSize, _Symbol, bid, sl, tp, "XAUUSD_SELL"))
      Print("[EA] Sell 失敗: ", g_trade.ResultRetcode());
   else
      Print("[EA] Sell 執行  bid=", bid, " SL=", sl, " TP=", tp);
}

//+------------------------------------------------------------------+
void UpdateTrailing(double atr, double trail_multi)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic()  != InpMagic)  continue;
      if(g_pos.Symbol() != _Symbol)   continue;

      double cur_sl = g_pos.StopLoss();
      double cur_tp = g_pos.TakeProfit();
      double trail  = atr * trail_multi;

      if(g_pos.PositionType() == POSITION_TYPE_BUY)
      {
         double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double new_sl = NormalizeDouble(bid - trail, _Digits);
         if(new_sl > cur_sl + _Point)
         {
            g_trade.PositionModify(g_pos.Ticket(), new_sl, cur_tp);
            if(InpDebugLog) Print("[EA] Trail BUY ", cur_sl, "→", new_sl);
         }
      }
      else if(g_pos.PositionType() == POSITION_TYPE_SELL)
      {
         double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double new_sl = NormalizeDouble(ask + trail, _Digits);
         if(new_sl < cur_sl - _Point)
         {
            g_trade.PositionModify(g_pos.Ticket(), new_sl, cur_tp);
            if(InpDebugLog) Print("[EA] Trail SELL ", cur_sl, "→", new_sl);
         }
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

//+------------------------------------------------------------------+
// signal.json 読み込み（簡易 JSON パーサ）
//+------------------------------------------------------------------+
bool ReadSignal(string &action, double &sl, double &tp,
                double &atr, double &atr_ratio, double &sl_multi,
                int &max_slip, double &rsi_exit, double &trail_m,
                string &ts)
{
   int fh = FileOpen(InpSignalFile, FILE_READ|FILE_TXT|FILE_ANSI);
   if(fh == INVALID_HANDLE) return false;
   string raw = "";
   while(!FileIsEnding(fh)) raw += FileReadString(fh);
   FileClose(fh);
   if(StringLen(raw) < 10) return false;

   action    = JStr(raw, "action");
   sl        = JDbl(raw, "sl_price");
   tp        = JDbl(raw, "tp_price");
   atr       = JDbl(raw, "atr");
   atr_ratio = JDbl(raw, "atr_ratio");
   sl_multi  = JDbl(raw, "sl_multi");
   max_slip  = (int)JDbl(raw, "max_slip_pt");
   rsi_exit  = JDbl(raw, "rsi_exit_thr");
   trail_m   = JDbl(raw, "trail_multi");
   ts        = JStr(raw, "timestamp");
   return StringLen(action) > 0;
}

string JStr(const string &j, const string &k)
{
   string pat = "\"" + k + "\":\"";
   int p = StringFind(j, pat);
   if(p < 0) return "";
   int s = p + StringLen(pat);
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
void WriteState()
{
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   int    pos = CountPos();
   string json = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"positions\":%d,"
      "\"timestamp\":\"%s\",\"symbol\":\"%s\",\"magic\":%d}",
      bal, eq, pos,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
      _Symbol, InpMagic);

   int fh = FileOpen(InpStateFile, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(fh == INVALID_HANDLE) return;
   FileWriteString(fh, json);
   FileClose(fh);
}
//+------------------------------------------------------------------+
