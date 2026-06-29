# Kalshi Bot

A small, transparent automated trading bot for [Kalshi](https://kalshi.com).
It is built to be **safe to learn with**: it defaults to fake money, it
will not place a single order unless you explicitly tell it to, and it has
hard money limits baked in.

> **Honest expectations (read this first).** Kalshi charges a trading fee on
> most markets (roughly `7% x price x (1 - price)` per contract as a taker,
> about a quarter of that for resting limit orders). Trading 20 times a day
> does **not** by itself make money — without a real edge, more trades just
> means more fees. The people posting big wins online are a heavily filtered
> sample. The plan here is to prove the bot works on the **demo (fake-money)
> environment first**, look at the results honestly, and only then risk real
> money. This is software for learning and experimenting, not financial
> advice, and not a guarantee of profit.

---

## What it does

Every cycle (default: once a minute) the bot:

1. Checks your balance and current positions.
2. Scans the most active markets and keeps the ones with a tradable spread.
3. Asks the chosen **strategy** what to do (the default is a conservative
   "maker" strategy that only acts when the spread is wide enough to beat
   fees).
4. Runs every proposed order through a **risk manager** (caps on position
   size, total exposure, daily loss, trades per day, and a cash reserve).
5. Either **logs what it would do** (dry run) or **places the order**.
6. Writes everything to `logs/trades.csv` so you can see exactly what
   happened and why.

---

## Step-by-step setup (no coding experience needed)

### 1. Install Python

If you don't already have it, download Python 3.10 or newer from
<https://www.python.org/downloads/>. During install on Windows, tick
**"Add Python to PATH."**

To check it worked, open a terminal (Command Prompt on Windows, Terminal on
Mac) and type:

```
python --version
```

You should see something like `Python 3.12.x`.

### 2. Open this folder in a terminal

In the terminal, move into this project folder:

```
cd "Kalshi Bot"
```

(Use the full path if needed, e.g. `cd "C:\Users\tanne\Claude\Projects\Kalshi Bot"`.)

### 3. Install the bot's requirements

```
pip install -r requirements.txt
```

### 4. Make a Kalshi DEMO account and API key

The demo environment is free fake money — perfect for testing.

1. Go to <https://demo.kalshi.co/> and create a demo account.
2. Inside the demo site, go to **Account & security -> API Keys**.
3. Click **Create Key**.
4. Save two things:
   - The **API Key ID** (a long code shown on screen).
   - The **Private Key** file it downloads (ends in `.key`). Put this file
     inside this project folder.

> Demo keys only work on demo, and real keys only work on the real site —
> they are never shared, which is a good safety feature.

### 5. Create your config file

1. Make a copy of `config.example.yaml` and name the copy `config.yaml`.
2. Open `config.yaml` in any text editor and fill in:
   - `key_id:` -> paste your API Key ID.
   - `private_key_path:` -> the name of your `.key` file (e.g. `kalshi-demo.key`).
3. Leave `environment: demo` for now.

### 6. Test the connection

```
python run.py check
```

You should see your demo balance and a few sample markets. If you get an
auth error, double-check the key id and the key file name in `config.yaml`.

### 7. Do a dry run (places NOTHING)

```
python run.py once
```

This runs one cycle and prints what it *would* do, marked `[DRY]`. Nothing
is sent. Read the output and the new `logs/trades.csv` to understand its
decisions.

### 8. Let it trade fake money on demo

When you're comfortable, let it actually place orders **on demo**:

```
python run.py once --execute        # one cycle
python run.py run --execute         # loop forever (stop with Ctrl+C)
```

Because your config says `environment: demo`, this is all fake money. Let it
run for a while, then look at `logs/trades.csv` and your demo balance to see
whether the strategy actually came out ahead **after fees**.

### 9. Going live (only if demo results are genuinely good)

Going live trades **real money**. Do this only after the demo results
convince you, and start with the smallest amount you're willing to lose.

1. Create a **real** API key at <https://kalshi.com> (Account & security ->
   API Keys) and download its `.key` file.
2. In `config.yaml`: set `environment: live`, update `key_id` and
   `private_key_path` to the real key.
3. Run with both safety flags:

```
python run.py run --execute --i-understand-live
```

The bot refuses to trade real money unless **both** `--execute` and
`--i-understand-live` are present. The same dollar limits in `config.yaml`
still apply.

---

## The safety limits (in `config.yaml`)

| Setting | What it does |
| --- | --- |
| `max_trades_per_day` | Hard stop on number of orders per day (default 20). |
| `max_position_dollars` | Most you'll ever hold in one market. |
| `max_open_dollars` | Most total capital deployed at once. |
| `max_daily_loss_dollars` | If you're down this much today, the bot stops. |
| `min_cash_reserve_dollars` | A floor it will never spend below. |

With a $20 account these are set small on purpose. Adjust them only when you
understand the trade-offs.

---

## Choosing a strategy

In `config.yaml`, set `strategy:` to one of:

- **`maker`** (default) — posts resting limit orders inside wide spreads and
  exits a little higher. Pays the low maker fee. Conservative.
- **`momentum`** — an *example* signal strategy that buys when YES has been
  rising. It's there to show how to wire in real signals (sports, crypto,
  your own model) later. Not a proven edge — keep it on demo.

You can add your own strategy by copying `kalshibot/strategies/maker.py`,
giving it a new name, and registering it in
`kalshibot/strategies/__init__.py`.

---

## Checking the math yourself

The fee math and all the safety limits have unit tests. Run them anytime:

```
python tests/test_logic.py
```

All 12 should report `PASS`.

---

## Project layout

```
Kalshi Bot/
  run.py                      <- the command you run
  config.example.yaml         <- copy to config.yaml and fill in
  requirements.txt
  README.md
  kalshibot/
    config.py                 <- loads your settings
    client.py                 <- talks to Kalshi (signed requests)
    fees.py                   <- fee calculations
    risk.py                   <- the safety limits
    journal.py                <- writes logs/trades.csv
    engine.py                 <- the main loop
    strategies/
      base.py                 <- the shared strategy interface
      maker.py                <- default conservative strategy
      momentum.py             <- example signal strategy
  tests/
    test_logic.py             <- proves the math + limits are correct
```

---

## A few important caveats

- **This is a starting point, not a finished money-maker.** The default
  strategy is simple and honest about it. Treat demo results as data, not as
  a promise.
- **Position average prices** are estimated from Kalshi's position data. For
  precise profit tracking you may want to record your own fills over time —
  a natural next improvement.
- **Always confirm the current fee schedule and API details** at
  <https://docs.kalshi.com>, since they can change.
- **Never commit `config.yaml` or your `.key` file anywhere public.** The
  included `.gitignore` already excludes them.
```
