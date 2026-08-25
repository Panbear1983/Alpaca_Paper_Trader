import wallets
wallets.apply('High Risk')
import hermes_report as hr
import time

for i in range(20):
    try:
        hr.fetch_account()
    except Exception as e:
        print(f"Account error: {e}")
    try:
        hr.fetch_positions()
    except Exception as e:
        print(f"Positions error: {e}")
    try:
        hr.fetch_open_orders()
    except Exception as e:
        print(f"Orders error: {e}")
    time.sleep(1)
print("Done")
