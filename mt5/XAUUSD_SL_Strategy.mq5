//+------------------------------------------------------------------+
//| XAUUSD_SL_Strategy.mq5                                           |
//| Python ブリッジ連携 BUY / SELL 対応 EA (UI表示機能追加版)          |
//+------------------------------------------------------------------+
#property copyright "XAUUSD SL Strategy"
#property version   "3.10"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//--- 入力パラメータ
input string InpSignalFile    = "signal.json";
input string InpStateFile     = "ea_state.json";
input string InpResetFile     = "ea_reset.json";
input double InpLotSize       = 0.05;    
input int    InpTimerSec      = 5;
input int    InpMagic         = 20240101;
input int    InpMinScore      = 30;      
input int    InpMaxConsecLoss = 3;       
input double InpTpMulti       = 3.0;    
input double InpTpMaxExt      = 2.0;    
input bool   InpDebugLog      = true;

// UI用定数
#define UI_OBJ_NAME "EA_Signal_Monitor"

CTrade        g_trade;
CPositionInfo g_pos;
string        g_signal_file = "";
string        g_state_file  = "";
string        g_reset_file  = "";
datetime      g_last_ts     = 0;
datetime      g_reset_since = 0;
double        g_lot_size    = 0.0;

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   EventSetTimer(InpTimerSec);
   
   if(StringCompare(InpSignalFile, "signal.json") == 0)
      g_signal_file = StringFormat("signal_%s.json", _Symbol);
   else
      g_signal_file = InpSignalFile;

   if(StringCompare(InpStateFile, "ea_state.json") == 0)
      g_state_file = StringFormat("ea_state_%s.json", _Symbol);
   else
      g_state_file = InpStateFile;

   if(StringCompare(InpResetFile, "ea_reset.json") == 0)
      g_reset_file = StringFormat("ea_reset_%s.json", _Symbol);
   else
      g_reset_file = InpResetFile;

   if(InpDebugLog)
      Print("[EA] 起動  Signal=", g_signal_file, " MinScore=", InpMinScore);
      
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectDelete(0, UI_OBJ_NAME); // チャート上のUIを削除
   if(InpDebugLog) Print("[EA] 停止 reason=", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   string action, sig_ts, strength, limit_prices_str = "";
   double sl_price, tp_price, atr_v, sl_multi, rsi_exit, trail_m, lot_sig;
   int    max_slip, score, tp_hold_min, max_pos;

   // シグナルファイルの読み込み
   bool read_ok = ReadSignal(action, sl_price, tp_price, atr_v, sl_multi,
                            max_slip, rsi_exit, trail_m, lot_sig, score, strength,
                            tp_hold_min, sig_ts, max_pos, limit_prices_str);

   // --- 【新規】UI表示の更新 ---
   UpdateSignalUI(read_ok, action, score, strength);

   if(!read_ok)
   {
      if(InpDebugLog) Print("[EA] signal.json 読み込み失敗");
      return;
   }

   // トレーリング SL + TP 動的延長
   if(atr_v > 0.0)
   {
      UpdateTrailing(atr_v, trail_m);
      UpdateTP(atr_v);
   }

   // 新規エントリー判定
   datetime sig_dt = StringToTime(sig_ts);
   if(sig_dt <= g_last_ts)
   {
      WriteState(GetConsecLosses());
      return;
   }
   g_last_ts = sig_dt;

   g_lot_size = (lot_sig > 0.0) ? lot_sig : InpLotSize;
   if(max_slip > 0) g_trade.SetDeviationInPoints(max_slip);

   // リセットチェック
   if(ReadResetState(g_reset_since) && InpDebugLog)
      Print("[EA] reset_losses 検出");

   int consec = GetConsecLosses();
   if(consec >= InpMaxConsecLoss)
   {
      if(InpDebugLog) Print("[EA] 連続損失上限。停止中");
      WriteState(consec);
      return;
   }

   if(score < InpMinScore)
   {
      if(InpDebugLog) Print("[EA] スコア不足: ", score);
      WriteState(consec);
      return;
   }

   // --- OnTimer内 ---
   // 新規エントリー（全ポジション数が max_pos 未満の時のみ）
   if(CountAllPos() < max_pos)
   {
      if(action == "buy")  OpenBuy(sl_price,  tp_price, score);  // scoreを追加
      if(action == "sell") OpenSell(sl_price, tp_price, score); // scoreを追加
      if(action == "limit_buy") OpenLimitBuy(limit_prices_str, sl_price, tp_price, score);
   }

   WriteState(consec);
}

//+------------------------------------------------------------------+
//| UI表示更新用関数                                                  |
//+------------------------------------------------------------------+
void UpdateSignalUI(bool success, string action, int score, string strength)
{
   string text;
   color  col = clrDarkGray;

   if(!success || (action != "buy" && action != "sell" && action != "limit_buy"))
   {
      text = "Signal: Scanning... (Wait)";
   }
   else
   {
      string act_upper = action; 
      StringToUpper(act_upper);
      text = StringFormat("Signal: %s | Score: %d | Power: %s", act_upper, score, strength);
      col  = (action == "buy" || action == "limit_buy") ? clrDeepSkyBlue : clrTomato;
    
   }

   // ラベルの作成・更新
   if(ObjectFind(0, UI_OBJ_NAME) < 0)
   {
      ObjectCreate(0, UI_OBJ_NAME, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, UI_OBJ_NAME, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, UI_OBJ_NAME, OBJPROP_XDISTANCE, 20);
      ObjectSetInteger(0, UI_OBJ_NAME, OBJPROP_YDISTANCE, 40);
      ObjectSetInteger(0, UI_OBJ_NAME, OBJPROP_FONTSIZE, 12);
      ObjectSetString(0, UI_OBJ_NAME, OBJPROP_FONT, "Arial Bold");
   }
   
   ObjectSetString(0, UI_OBJ_NAME, OBJPROP_TEXT, text);
   ObjectSetInteger(0, UI_OBJ_NAME, OBJPROP_COLOR, col);
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| 以下、コアロジック関数 (変更なし)                                  |
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
// エントリー関数（スマホ通知付き）
//+------------------------------------------------------------------+
void OpenBuy(double sl, double tp, int score) // scoreを引数に追加
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(sl >= ask) return;

   if(g_trade.Buy(g_lot_size, _Symbol, 0, sl, tp, "SL_BUY"))
   {
      // ★スマホへのプッシュ通知
      string msg = StringFormat("[EA] %s BUY発火! Score:%d, Lot:%.2f", _Symbol, score, g_lot_size);
      SendNotification(msg);
      
      if(InpDebugLog) Print(msg);
   }
   else
      Print("[EA] Buy 失敗: ", g_trade.ResultComment());
}

void OpenLimitBuy(string limit_prices_str, double sl, double tp, int score)
{
   // limit_prices_str: "[100.0, 101.0, 102.0]"
   if(StringLen(limit_prices_str) < 5) return;
   
   // [ と ] を除去
   string clean = StringSubstr(limit_prices_str, 1, StringLen(limit_prices_str) - 2);
   string parts[];
   int count = StringSplit(clean, ',', parts);
   if(count != 3) return;
   
   double prices[3];
   for(int i = 0; i < 3; i++)
   {
      prices[i] = StringToDouble(parts[i]);
   }
   
   // 3つのリミット買い注文を入れる
   for(int i = 0; i < 3; i++)
   {
      if(g_trade.BuyLimit(g_lot_size, prices[i], _Symbol, sl, tp, ORDER_TIME_GTC, 0, "LIMIT_BUY"))
      {
         string msg = StringFormat("[EA] %s LIMIT_BUY発火! Price:%.2f, Score:%d, Lot:%.2f", _Symbol, prices[i], score, g_lot_size);
         SendNotification(msg);
         if(InpDebugLog) Print(msg);
      }
      else
      {
         Print("[EA] LimitBuy 失敗: ", g_trade.ResultComment());
      }
   }
}

void OpenSell(double sl, double tp, int score) // scoreを引数に追加
{
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(sl <= ask) return;

   if(g_trade.Sell(g_lot_size, _Symbol, 0, sl, tp, "SL_SELL"))
   {
      // ★スマホへのプッシュ通知
      string msg = StringFormat("[EA] %s SELL発火! Score:%d, Lot:%.2f", _Symbol, score, g_lot_size);
      SendNotification(msg);

      if(InpDebugLog) Print(msg);
   }
   else
      Print("[EA] Sell 失敗: ", g_trade.ResultComment());
}

void UpdateTrailing(double atr, double trail_multi)
{
   if(trail_multi <= 0.0) return;
   double trail = atr * trail_multi;

   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i) || g_pos.Magic() != InpMagic || g_pos.Symbol() != _Symbol) continue;

      double cur_sl = g_pos.StopLoss();
      double cur_tp = g_pos.TakeProfit();
      double new_sl = cur_sl;

      if(g_pos.PositionType() == POSITION_TYPE_BUY)
      {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double calc = NormalizeDouble(bid - trail, _Digits);
         if(calc > cur_sl + _Point) new_sl = calc;
      }
      else if(g_pos.PositionType() == POSITION_TYPE_SELL)
      {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double calc = NormalizeDouble(ask + trail, _Digits);
         if(calc < cur_sl - _Point) new_sl = calc;
      }

      if(new_sl != cur_sl) g_trade.PositionModify(g_pos.Ticket(), new_sl, cur_tp);
   }
}

void UpdateTP(double atr)
{
   for(int i = PositionsTotal()-1; i >= 0; i--)
   {
      if(!g_pos.SelectByIndex(i) || g_pos.Magic() != InpMagic || g_pos.Symbol() != _Symbol) continue;

      double cur_tp = g_pos.TakeProfit();
      double cur_sl = g_pos.StopLoss();
      double entry  = g_pos.PriceOpen();
      double new_tp = cur_tp;

      if(g_pos.PositionType() == POSITION_TYPE_BUY)
      {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         double profit_atr = (bid - entry) / atr;
         if(profit_atr >= 1.0)
         {
            double ext = MathMin(MathFloor(profit_atr), InpTpMaxExt);
            double calc = NormalizeDouble(entry + atr * (InpTpMulti + ext), _Digits);
            if(calc > cur_tp + _Point) new_tp = calc;
         }
      }
      else if(g_pos.PositionType() == POSITION_TYPE_SELL)
      {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double profit_atr = (entry - ask) / atr;
         if(profit_atr >= 1.0)
         {
            double ext = MathMin(MathFloor(profit_atr), InpTpMaxExt);
            double calc = NormalizeDouble(entry - atr * (InpTpMulti + ext), _Digits);
            if(calc < cur_tp - _Point) new_tp = calc;
         }
      }

      if(new_tp != cur_tp) g_trade.PositionModify(g_pos.Ticket(), cur_sl, new_tp);
   }
}

int CountPos()
{
   int cnt = 0;
   for(int i = PositionsTotal()-1; i >= 0; i--)
      if(g_pos.SelectByIndex(i) && g_pos.Magic()==InpMagic && g_pos.Symbol()==_Symbol) cnt++;
   return cnt;
}

int CountAllPos() { return PositionsTotal(); }

int GetConsecLosses()
{
   int consec = 0;
   if(!HistorySelect(0, TimeCurrent())) return 0;
   int total = HistoryDealsTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(ticket, DEAL_MAGIC) != InpMagic || HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
      if(HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      datetime deal_time = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(g_reset_since > 0 && deal_time < g_reset_since) break;
      double profit = HistoryDealGetDouble(ticket, DEAL_PROFIT) + HistoryDealGetDouble(ticket, DEAL_SWAP) + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      if(profit < 0.0) consec++; else break;
   }
   return consec;
}

bool ReadSignal(string &action, double &sl, double &tp, double &atr, double &sl_multi, int &max_slip, double &rsi_exit, double &trail_m, double &lot_size, int &score, string &strength, int &tp_hold_min, string &ts, int &max_pos, string &limit_prices_str)
{
   int fh = FileOpen(g_signal_file, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
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
   max_pos     = (int)JDbl(raw, "max_positions");
   limit_prices_str = JStr(raw, "limit_prices");
   if(max_pos <= 0) max_pos = 1;
   return StringLen(action) > 0;
}

bool ReadResetState(datetime &reset_since)
{
   if(!FileIsExist(g_reset_file, FILE_COMMON)) return false;
   int fh = FileOpen(g_reset_file, FILE_READ|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh == INVALID_HANDLE) { FileDelete(g_reset_file, FILE_COMMON); return false; }
   string raw = ""; while(!FileIsEnding(fh)) raw += FileReadString(fh); FileClose(fh);
   reset_since = (datetime)JDbl(raw, "reset_since");
   FileDelete(g_reset_file, FILE_COMMON);
   return reset_since > 0;
}

string JStr(const string &j, const string &k) {
   string pat = "\"" + k + "\":"; int p = StringFind(j, pat); if(p < 0) return "";
   int s = p + StringLen(pat); while(s < StringLen(j) && StringSubstr(j,s,1)==" ") s++;
   if(StringSubstr(j,s,1) != "\"") return ""; s++;
   int e = StringFind(j, "\"", s); return (e < 0) ? "" : StringSubstr(j, s, e-s);
}

double JDbl(const string &j, const string &k) {
   string pat = "\"" + k + "\":"; int p = StringFind(j, pat); if(p < 0) return 0.0;
   int s = p + StringLen(pat); while(s < StringLen(j) && StringSubstr(j,s,1)==" ") s++;
   int e = s; while(e < StringLen(j)) {
      string c = StringSubstr(j, e, 1); if(c==","||c=="}"||c=="\n"||c==" "||c=="\r") break; e++;
   }
   return StringToDouble(StringSubstr(j, s, e-s));
}

void WriteState(int consec_losses)
{
   string json = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"positions\":%d,\"consecutive_losses\":%d,\"timestamp\":\"%s\",\"symbol\":\"%s\",\"magic\":%d}",
      AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY), CountPos(), consec_losses,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS), _Symbol, InpMagic);
   int fh = FileOpen(g_state_file, FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON);
   if(fh != INVALID_HANDLE) { FileWriteString(fh, json); FileClose(fh); }
}

