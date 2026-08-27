"""
ASGI entry point, so the app can be served without importing the demo.

    set RAZORPAY_WEBHOOK_SECRET=whsec_...
    uvicorn webhook_app:app --app-dir src --port 8000

The store starts empty on purpose: an empty store resolves no fund accounts, so
every payout is held. Seeding it with real vendor and fund-account data is the
merchant's integration step, not something this file should guess at.
"""

from webhook import Store, create_app

store = Store()
app = create_app(store)
