import alpaca_trade_api as tradeapi
api = tradeapi.REST()
acc = api.get_account()
print('Equity:', acc.equity)
print('Last equity:', getattr(acc, 'last_equity', 'N/A'))
print('Today PL:', getattr(acc, 'today_pl', 'N/A'))
print('Today PL %:', getattr(acc, 'today_pl_pc', 'N/A'))
print('Cash:', acc.cash)
print('Buying power:', acc.buying_power)
positions = api.list_positions()
print('Number of positions:', len(positions))
for p in positions:
    print(f'  {p.symbol}: {p.qty} shares @ {p.avg_entry_price}, current price: {p.current_price}, unrealized pl: {p.unrealized_pl}')
