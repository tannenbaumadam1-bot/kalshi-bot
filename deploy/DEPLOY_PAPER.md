# Run the paper bot online, 24/7

This puts your **paper-trading bot + dashboard** on a small cloud server so it
runs around the clock and you can check it from your phone — even with your
computer off. No API key, no money, nothing sensitive is uploaded.

Total time: ~15 minutes. Cost: ~$5/month for the server.

You do 3 things; the script does the rest.

---

## Step 1 — Create the server (your part — needs a card)

1. Go to a cloud provider. Good cheap options: **DigitalOcean**, **Vultr**, or **Hetzner**.
2. Create an account (this is the step I can't do for you — it needs your email + a card).
3. Create the smallest **Ubuntu 24.04** server (often called a "Droplet" / "Instance" / "VPS"). The ~$4–6/month size is plenty.
4. When it's made, the provider shows you:
   - the server's **IP address** (like `203.0.113.7`)
   - a way to log in: either a **web console** (a black terminal in your browser) or a password/SSH.

Tip: the **web console** in your provider's dashboard is the easiest — no extra software.

---

## Step 2 — Upload the bot (one file)

You have one file from me: **`kalshibot_cloud.tar.gz`**.

Easiest way (Windows): open PowerShell on your computer and run (replace the IP):

```
scp "kalshibot_cloud.tar.gz" root@YOUR_SERVER_IP:/root/
```

(If `scp` asks to continue, type `yes`; then enter the server password.)

No `scp`? Use your provider's file upload, or ask me and I'll walk you through it.

---

## Step 3 — Run one command (paste it in)

Log into the server (web console or SSH), then paste these three lines:

```
sudo mkdir -p /opt/kalshibot
sudo tar -xzf /root/kalshibot_cloud.tar.gz -C /opt/kalshibot
sudo bash /opt/kalshibot/deploy/setup_paper_server.sh
```

It installs everything, starts the bot + dashboard, and prints a link like:

```
http://YOUR_SERVER_IP:8765/?token=AbC123xyz789
```

**That link is your live dashboard.** Open it on your phone or any browser.
Keep it private — the token at the end is its password.

---

## That's it

- The bot now runs 24/7 and restarts itself if the server reboots.
- Watch it anytime at your dashboard link.

### Handy commands (paste on the server)

```
systemctl status kalshi-paper        # is the bot running?
journalctl -u kalshi-paper -f        # watch the bot live (Ctrl+C to stop watching)
systemctl restart kalshi-paper       # restart the bot
systemctl stop kalshi-paper          # pause the bot
```

### To turn it all off
Delete the server in your provider's dashboard — billing stops immediately.
