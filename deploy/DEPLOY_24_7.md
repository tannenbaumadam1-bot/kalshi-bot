# Running the bot 24/7 (so it runs even when your laptop is off)

Your laptop can't do this — it stops when it sleeps. To run around the
clock you put the bot on a small always-on computer in the cloud (a
"server"). Cost: about **$4–6 a month**. It's still the DEMO (fake money),
so nothing real is at risk while we test.

> You'll need to do the account/payment/SSH steps yourself (I can't create
> accounts or log in as you), but I'll guide every step. Ask me whenever
> you get stuck and paste what you see.

---

## The easiest beginner option (recommended): a cloud VPS

### 1. Make a server
- Sign up at **DigitalOcean** (or Linode/Vultr/AWS Lightsail).
- Create the smallest **Ubuntu** server (a "Droplet"), ~$4–6/mo.
- Choose **password** or SSH-key login and save the details.

### 2. Get the bot onto the server
You need to copy this whole `Kalshi Bot` folder up to the server. Two ways:
- **Easiest:** put the folder in a **private GitHub repo** and `git clone`
  it on the server. (Never use a public repo — your key file is secret.)
- **Or** use an SFTP app like **FileZilla** or **WinSCP** to drag the
  folder onto the server.

Make sure `kalshi-demo.key` and `config.yaml` come along.

### 3. Set it up (one time)
SSH into the server, then run:
```
cd kalshi-bot
bash deploy/setup_server.sh
```
This installs Python and tests the connection. If it prints your balance,
you're good.

### 4. Turn on the 24/7 service
```
sudo cp deploy/kalshibot.service /etc/systemd/system/
sudo nano /etc/systemd/system/kalshibot.service   # fix the two CHANGE-ME paths
sudo systemctl daemon-reload
sudo systemctl enable --now kalshibot
```
Now it runs continuously and restarts itself if it ever crashes or the
server reboots.

### 5. Check on it anytime
```
systemctl status kalshibot        # is it running?
tail -f logs/bot.out              # watch what it's doing live
```

---

## Security (important)
- Your `kalshi-demo.key` is a **secret** — keep the repo private, and don't
  share the server login.
- For DEMO this is low-stakes. **Before ever using a real-money key**, lock
  the server down (firewall, SSH keys only, no password login) — ask me and
  I'll walk you through hardening it.

---

## Even simpler (no server admin): managed Python hosts
If SSH feels like too much, beginner-friendly hosts can run a Python script
24/7 for a few dollars a month — e.g. **PythonAnywhere** ("always-on task"),
**Railway**, or **Render**. You upload the code, set the start command to
`python run.py run --execute`, and add your key as a secret file. Tell me
which one you pick and I'll give you the exact steps for it.

---

## My honest recommendation
Get this running 24/7 on **demo**, let the smart strategy run for a week or
two, and let the daily report show what actually happens — wins, losses,
fees. THEN we improve the strategy based on real data, not guesses. That's
how you find out if it really works.
