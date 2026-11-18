//+------------------------------------------------------------------+
//| DemoAutoTrade.mq5                                                |
//| Places ONE market order on a DEMO account once the terminal is   |
//| connected, then stops. Refuses to trade on any non-demo account. |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input string InpSymbol = "XAUUSD";   // symbol to trade (falls back to any XAU symbol)
input double InpLots   = 0.01;       // lot size (raised to the broker minimum if needed)
input bool   InpBuy    = true;       // true = buy, false = sell
input ulong  InpMagic  = 20261118;   // magic number

CTrade g_trade;
bool   g_done   = false;
int    g_tries  = 0;
string g_symbol = "";

void WriteResult(const string text)
{
   int h = FileOpen("trade_result.txt", FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, text + "\r\n");
      FileClose(h);
   }
}

string ResolveSymbol()
{
   if(SymbolSelect(InpSymbol, true))
      return InpSymbol;

   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
   {
      string name = SymbolName(i, false);
      if(StringFind(name, "XAU") >= 0 && SymbolSelect(name, true))
         return name;
   }
   return "";
}

int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(30);
   EventSetTimer(5);
   Print("DemoAutoTrade started. Waiting for the terminal to connect...");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   if(g_done)
      return;

   g_tries++;
   if(g_tries > 36)
   {
      Print("GAVE UP after 36 attempts. No trade placed.");
      WriteResult("FAILED: gave up after 36 attempts");
      g_done = true;
      return;
   }

   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
   {
      Print("Waiting: terminal not connected to the broker yet");
      return;
   }

   // The account mode reads as 0 (= demo) before login, so require a real login first.
   if(AccountInfoInteger(ACCOUNT_LOGIN) <= 0)
   {
      Print("Waiting: no account logged in yet");
      return;
   }

   if((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("SAFETY STOP: this is not a demo account. No trade will be placed.");
      WriteResult("SAFETY STOP: not a demo account");
      g_done = true;
      return;
   }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      Print("Waiting: algorithmic trading is not allowed yet");
      return;
   }

   if(g_symbol == "")
      g_symbol = ResolveSymbol();
   if(g_symbol == "")
   {
      Print("Waiting: symbol not found on this server");
      return;
   }

   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick) || tick.ask <= 0.0)
   {
      Print("Waiting: no tick yet for ", g_symbol);
      return;
   }

   double minLot = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_MIN);
   double step   = SymbolInfoDouble(g_symbol, SYMBOL_VOLUME_STEP);
   double lots   = MathMax(InpLots, minLot);
   if(step > 0.0)
      lots = MathFloor(lots / step + 0.0000001) * step;
   lots = NormalizeDouble(lots, 2);

   g_trade.SetTypeFillingBySymbol(g_symbol);
   bool ok = InpBuy ? g_trade.Buy(lots, g_symbol, 0.0, 0.0, 0.0, "gh-ea-demo")
                    : g_trade.Sell(lots, g_symbol, 0.0, 0.0, 0.0, "gh-ea-demo");

   uint rc = g_trade.ResultRetcode();
   Print("Order result: retcode=", rc, " (", g_trade.ResultRetcodeDescription(),
         ") order=", g_trade.ResultOrder(), " deal=", g_trade.ResultDeal());

   if(ok && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_PLACED))
   {
      Print("TRADE PLACED on ", g_symbol, " lots=", DoubleToString(lots, 2));
      WriteResult("TRADE PLACED on " + g_symbol + " lots=" + DoubleToString(lots, 2) +
                  " retcode=" + IntegerToString((int)rc));
      g_done = true;
   }
   else
   {
      Print("Order not accepted, will retry. Attempt ", g_tries, " of 36");
   }
}
