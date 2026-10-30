# WISE VIRGINS

```
pip install fastapi uvicorn httpx
REPO_URL=https://github.com/you/your-repo uvicorn app:app --reload
```
Open http://localhost:8000. Default `PAYMENT_MODE=mock` shows a dev button to simulate payment.

## Going live with M-Pesa
Set `PAYMENT_MODE=daraja`, `PUBLIC_URL` (https), `CALLBACK_SECRET` (random), `DARAJA_KEY`, `DARAJA_SECRET`,
`DARAJA_SHORTCODE`, `DARAJA_PASSKEY`, and `DARAJA_BASE=https://api.safaricom.co.ke`.
- Daraja STK Push needs a registered Paybill or Till. A personal number such as 0105636227 cannot receive Daraja payments;
  the number shown on screen is cosmetic (`PAY_NUMBER`) and should match your Till/Paybill details.
- Payments are marked paid only by Safaricom's callback, with the amount checked against the package price.
- Run behind HTTPS (Caddy/nginx). Add a rate limiter before launch.

## Editing packages
`PACKAGES` in `app.py` sets price, link opens (`uses`) and lifetime (`days`). Those terms are my assumptions; change them.

## Postgres later
All SQL is plain; swap `sqlite3` for `psycopg` and replace `INSERT OR REPLACE` with `ON CONFLICT`.
